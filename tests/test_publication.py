import copy
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from publisher.publication import finalize, garbage_collect, prepare, read_release, upload_shard
from publisher.resources import migration_candidate, normalize
from publisher.storage import Storage, digest, encode, owned_key


class S3:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.reads = []
        self.fail_key = None
        self.race = None

    def seed(self, key, body, *, days=0, metadata=None):
        self.objects[key] = {"Body": body, "ETag": '"' + digest(body) + '"', "ContentLength": len(body),
                             "Metadata": metadata or {}, "LastModified": datetime.now(timezone.utc) - timedelta(days=days)}
        return self.objects[key]["ETag"]

    def error(self, status):
        raise ClientError({"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}, "S3")

    def get_object(self, Bucket, Key, IfMatch=None):
        row = self.head_object(Bucket=Bucket, Key=Key)
        if IfMatch and row["ETag"] != IfMatch:
            self.error(412)
        self.reads.append(Key)
        return {**row, "Body": io.BytesIO(self.objects[Key]["Body"])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            self.error(404)
        return {name: value for name, value in self.objects[Key].items() if name != "Body"}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **kwargs):
        if self.race and Key.endswith("/latest.json"):
            self.seed(Key, self.race)
            self.race = None
        if Key == self.fail_key:
            self.error(503)
        if IfNoneMatch and Key in self.objects:
            self.error(412)
        if IfMatch and self.objects.get(Key, {}).get("ETag") != IfMatch:
            self.error(412)
        data = Body.read() if hasattr(Body, "read") else Body
        self.writes.append(("put", Key, len(data)))
        return {"ETag": self.seed(Key, data, metadata=kwargs.get("Metadata"))}

    def copy_object(self, Bucket, Key, CopySource, CopySourceIfMatch, **kwargs):
        source = self.objects[CopySource["Key"]]
        if source["ETag"] != CopySourceIfMatch:
            self.error(412)
        if Key == self.fail_key:
            self.error(503)
        self.writes.append(("copy", Key, len(source["Body"])))
        return {"CopyObjectResult": {"ETag": self.seed(Key, source["Body"], metadata=kwargs.get("Metadata"))}}

    def get_paginator(self, operation):
        return self

    def paginate(self, Bucket, Prefix):
        return [{"Contents": [{"Key": key, "Size": row["ContentLength"], "ETag": row["ETag"], "LastModified": row["LastModified"]}
                              for key, row in self.objects.items() if key.startswith(Prefix)]}]

    def delete_object(self, Bucket, Key, IfMatch):
        if self.objects[Key]["ETag"] != IfMatch:
            self.error(412)
        self.writes.append(("delete", Key, 0))
        del self.objects[Key]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s3 = S3()
        self.env = patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker-phigros-data", "S3_ENDPOINT": "https://s3.example", "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        mocked = patch("publisher.storage.boto3.client", return_value=self.s3)
        mocked.start()
        self.addCleanup(mocked.stop)

    def candidate(self, values=None, migration=False):
        values = values or {"catalog.json": b'{"songs":[]}', "metadata/note_counts.tsv": b"notes", "music/song.ogg": b"music", "charts/song.0/IN.json": b"chart"}
        assets = []
        for logical, data in values.items():
            row = {"logical": logical, "original": logical, "size": len(data), "sha256": digest(data)}
            if migration:
                key = "phigros/releases/4.0.1/" + logical
                etag = self.s3.seed(key, data)
                row["source"] = {"key": key, "etag": etag}
            else:
                path = self.root / "input" / logical
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                row["local"] = str(path)
            assets.append(row)
        return normalize("phigros", {"gameVersion": "4.0.1"}, assets, self.root / "metadata")

    def publish(self, candidate, migration=False):
        output = self.root / ("plan-" + str(len(self.s3.writes)))
        plan = prepare("phigros", candidate, output, migration=migration)
        receipts = [upload_shard(plan, row["id"], output / f"shard-{row['id']}", output / f"receipt-{row['id']}.json") for row in plan["shards"]]
        result = finalize(plan, receipts)
        return plan, result

    def test_initial_migration_copies_then_unchanged_reads_no_media(self):
        candidate = self.candidate(migration=True)
        plan, result = self.publish(candidate, migration=True)
        self.assertEqual(plan["summary"]["upload"]["count"], 0)
        self.assertEqual(plan["summary"]["copy"]["count"], 4)
        self.assertFalse(result["unchanged"])
        self.assertTrue(all(mode == "copy" or "/manifests/" in key or key.endswith("/latest.json") for mode, key, _ in self.s3.writes))
        self.s3.writes.clear()
        self.s3.reads.clear()
        _, result = self.publish(candidate)
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.s3.writes, [])
        self.assertTrue(all("/manifests/" in key or key.endswith("/latest.json") for key in self.s3.reads))

    def test_one_file_change_uploads_one_file_and_keeps_old_objects(self):
        old = self.candidate()
        self.publish(old, migration=True)
        old_keys = set(self.s3.objects)
        changed = self.candidate({"catalog.json": b'{"songs":[]}', "metadata/note_counts.tsv": b"notes", "music/song.ogg": b"new music", "charts/song.0/IN.json": b"chart"})
        self.s3.writes.clear()
        plan, result = self.publish(changed)
        self.assertEqual(plan["summary"]["upload"], {"count": 1, "bytes": 9})
        self.assertEqual(plan["summary"]["reuse"]["count"], 3)
        self.assertTrue(old_keys <= set(self.s3.objects))
        self.assertEqual(len([key for mode, key, _ in self.s3.writes if key.startswith("phigros/music/")]), 1)
        release = read_release(Storage("phigros"), "phigros")
        self.assertIsNotNone(release["manifest"]["previousManifest"])

    def test_missing_baseline_never_falls_back_to_full_upload(self):
        with self.assertRaisesRegex(ValueError, "Missing publication baseline"):
            prepare("phigros", self.candidate(), self.root / "plan")
        self.assertEqual(self.s3.writes, [])

    def test_failed_or_missing_child_does_not_switch_pointer(self):
        self.publish(self.candidate(), migration=True)
        original = self.s3.objects["phigros/latest.json"]["Body"]
        candidate = self.candidate({"catalog.json": b"catalog2", "metadata/note_counts.tsv": b"notes2"})
        plan = prepare("phigros", candidate, self.root / "plan")
        self.s3.fail_key = plan["tasks"][0]["key"]
        with self.assertRaises(ClientError):
            upload_shard(plan, plan["tasks"][0]["shard"], self.root / f"plan/shard-{plan['tasks'][0]['shard']}", self.root / "receipt.json")
        with self.assertRaisesRegex(ValueError, "child workflows"):
            finalize(plan, [])
        self.assertEqual(self.s3.objects["phigros/latest.json"]["Body"], original)

    def test_copy_source_change_fails_without_upload_fallback(self):
        candidate = self.candidate(migration=True)
        plan = prepare("phigros", candidate, self.root / "plan", migration=True)
        task = plan["tasks"][0]
        self.s3.seed(task["source"]["key"], b"changed")
        with self.assertRaises(ClientError):
            upload_shard(plan, task["shard"], self.root / "plan", self.root / "receipt.json")
        self.assertNotIn("phigros/latest.json", self.s3.objects)
        self.assertEqual(self.s3.writes, [])

    def test_pointer_compare_and_swap_conflict_preserves_winner(self):
        self.publish(self.candidate(), migration=True)
        winner = self.s3.objects["phigros/latest.json"]["Body"] + b" "
        candidate = self.candidate({"catalog.json": b"changed", "metadata/note_counts.tsv": b"notes"})
        self.s3.race = winner
        with self.assertRaises(ClientError):
            self.publish(candidate)
        self.assertEqual(self.s3.objects["phigros/latest.json"]["Body"], winner)

    def test_reused_destination_is_verified_before_pointer(self):
        candidate = self.candidate(migration=True)
        row = candidate["objects"][0]
        self.s3.seed(row["key"], b"x" * row["size"])
        with self.assertRaisesRegex(ValueError, "verification failed"):
            prepare("phigros", candidate, self.root / "plan", migration=True)
        self.assertEqual(self.s3.writes, [])

    def test_owned_paths_reject_other_groups_and_protected_objects(self):
        for key in ("fonts/a.woff", "chart-preview/x.png", "phigros/chapters.csv", "kyou/data/" + "a" * 64 + ".json", "phigros/music/../x.ogg", "phigros/music/a.ogg"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                owned_key("phigros", key)

    def test_storage_probe_checks_conditions_and_removes_only_its_scratch_objects(self):
        from publisher.storage_check import check
        self.s3.seed("phigros/chapters.csv", b"protected")
        self.assertTrue(check("phigros")["conditionalCopy"])
        self.assertEqual(list(self.s3.objects), ["phigros/chapters.csv"])

    def test_cleanup_keeps_current_previous_recent_and_shared_objects(self):
        keys = []
        for index, age in enumerate((20, 19, 10, 0)):
            candidate = self.candidate({"catalog.json": f"catalog-{index}".encode(), "metadata/note_counts.tsv": b"shared"})
            output = self.root / f"release-{index}"
            plan = prepare("phigros", candidate, output, migration=index == 0)
            plan["preparedAt"] = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
            receipts = [upload_shard(plan, row["id"], output / f"shard-{row['id']}", output / f"receipt-{row['id']}.json") for row in plan["shards"]]
            result = finalize(plan, receipts)
            for row in candidate["objects"]:
                if row["key"] not in keys:
                    self.s3.objects[row["key"]]["LastModified"] -= timedelta(days=age)
                    keys.append(row["key"])
            self.s3.objects[result["pointer"]["manifest"]]["LastModified"] -= timedelta(days=age)
        protected = ("phigros/chapters.csv", "phigros/releases/4.0.1/music/a.ogg", "chart-preview/x.ogg", "fonts/a.woff", "kyou/latest/songs.json")
        for key in protected:
            self.s3.seed(key, b"protected", days=30)
        rows = garbage_collect("phigros", execute=True)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(key in self.s3.objects for key in protected))
        self.assertIn(keys[-1], self.s3.objects)
        self.assertIn(keys[1], self.s3.objects)
        self.assertNotIn(keys[0], self.s3.objects)

    def test_migration_detects_missing_legacy_object_without_writes(self):
        data = b"resource"
        key = "phigros/releases/4.0.1/manifest.json"
        self.s3.seed("phigros/current.json", encode({"manifest": key}))
        self.s3.seed(key, encode({"gameVersion": "4.0.1", "assets": [{"path": "music/missing.ogg", "size": len(data), "sha256": digest(data)}]}))
        with self.assertRaisesRegex(ValueError, "baseline missing"):
            migration_candidate("phigros", self.root)
        self.assertEqual(self.s3.writes, [])


if __name__ == "__main__":
    unittest.main()
