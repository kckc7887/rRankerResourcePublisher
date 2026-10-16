import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from publisher.publication import finalize, prepare, upload_shard
from publisher.resources import KYOU_FILES, migration_candidate, normalize
from publisher.storage import digest, encode
from test_publication import S3
from test_rizline import PNG, sample_acb, overrides


class ResourceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s3 = S3()
        for context in (patch.dict(os.environ, {"S3_ENDPOINT": "https://example.test", "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test", "S3_BUCKET_NAME": "rranker-rizline-data"}),
                        patch("publisher.storage.boto3.client", return_value=self.s3)):
            context.start()
            self.addCleanup(context.stop)

    def test_rizline_migration_uploads_only_rewritten_catalog(self):
        prefix = "rizline/releases/2026-09-20-2/"
        chart, audio, cover = prefix + "charts/old.json", prefix + "audio/old.m4a", prefix + "covers/old.png"
        repeated_chart = prefix + "charts/same-content.json"
        catalog = {"schemaVersion": 1, "gameVersion": "2.7.1", "resourceVersion": "date", "songs": [
            {"id": "Song.A", "coverPath": cover, "audioPath": audio, "charts": [{"chartPath": chart}, {"chartPath": repeated_chart}]}]}
        payloads = {chart: b"chart", repeated_chart: b"chart", audio: b"audio", cover: b"cover", prefix + "catalog.json": encode(catalog)}
        files = [{"path": key, "size": len(data), "sha256": digest(data)} for key, data in payloads.items()]
        for key, data in payloads.items():
            self.s3.seed(key, data)
        manifest = encode({"gameVersion": "2.7.1", "catalogPath": prefix + "catalog.json", "files": files})
        self.s3.seed(prefix + "manifest.json", manifest)
        self.s3.seed("rizline/current.json", encode({"manifestPath": prefix + "manifest.json", "manifestSha256": digest(manifest)}))
        old = copy.deepcopy(self.s3.objects)
        candidate = migration_candidate("rizline", self.root / "metadata")
        plan = prepare("rizline", candidate, self.root / "plan", migration=True)
        self.assertEqual(plan["summary"]["copy"]["count"], 3)
        self.assertEqual(plan["summary"]["upload"]["count"], 1)
        receipts = [upload_shard(plan, row["id"], self.root / f"plan/shard-{row['id']}", self.root / f"receipt-{row['id']}.json") for row in plan["shards"]]
        finalize(plan, receipts)
        body = json.loads(self.s3.objects[candidate["manifest"]["catalogPath"]]["Body"])
        song = body["songs"][0]
        self.assertEqual(self.s3.objects[song["audioPath"]]["Body"], b"audio")
        self.assertEqual(self.s3.objects[song["charts"][0]["chartPath"]]["Body"], b"chart")
        self.assertEqual(song["charts"][0]["chartPath"], song["charts"][1]["chartPath"])
        self.assertEqual(len(candidate["manifest"]["files"]), 4)
        self.assertTrue(song["coverPath"].startswith("rizline/covers/"))
        for key, row in old.items():
            self.assertEqual(self.s3.objects[key], row)

    def test_kyou_timestamps_do_not_change_resource_identity(self):
        assets = [{"original": name, "logical": name, "size": 2, "sha256": digest(b"[]"), "local": "unused"} for name in KYOU_FILES]
        manifest = {"ok": True, "strategy": "top-batch", "charts_with_tags": 1, "charts_rows": 1, "finished_unix": 1}
        first = normalize("kyou", manifest, assets, self.root)
        second = normalize("kyou", {**manifest, "finished_unix": 2, "started_unix": 1.5}, assets, self.root)
        self.assertEqual(first["manifest"]["resourceVersion"], second["manifest"]["resourceVersion"])
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            normalize("kyou", {**manifest, "fallback_failed": 1}, assets, self.root)

    def test_kyou_migration_rejects_incomplete_stored_table(self):
        manifest = {"ok": True, "strategy": "top-batch", "songs_rows": 2, "aliases_rows": 0,
                    "charts_rows": 0, "charts_with_tags": 0, "tag_vote_rows": 0}
        self.s3.seed("kyou/latest/manifest.json", encode(manifest))
        for name in KYOU_FILES:
            self.s3.seed(f"kyou/latest/{name}", b"[]")
        with patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker-phigros-data"}):
            with self.assertRaisesRegex(ValueError, "Kyou row count mismatch: songs.json"):
                migration_candidate("kyou", self.root)
        self.assertEqual(self.s3.writes, [])

    def kyou_plan(self, value, migration=False):
        assets = []
        for name in KYOU_FILES:
            data = encode([value]) if name.endswith(".json") else f"value\n{value}\n".encode()
            path = self.root / value / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            assets.append({"original": name, "logical": name, "size": len(data), "sha256": digest(data), "local": str(path)})
        candidate = normalize("kyou", {"ok": True, "strategy": "top-batch", "charts_with_tags": 1, "charts_rows": 1}, assets, self.root / "metadata")
        output = self.root / ("plan-" + value)
        plan = prepare("kyou", candidate, output, migration=migration)
        receipts = [upload_shard(plan, row["id"], output / f"shard-{row['id']}", output / f"receipt-{row['id']}.json") for row in plan["shards"]]
        return plan, receipts

    @patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker-phigros-data", "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/publisher", "GITHUB_WORKFLOW_REF": "example/publisher/.github/workflows/kyou.yml@refs/heads/master"})
    def test_kyou_serialized_workflow_publishes_and_verifies_release(self):
        finalize(*self.kyou_plan("base", migration=True))
        plan, receipts = self.kyou_plan("updated")
        result = finalize(plan, receipts)
        pointer = json.loads(self.s3.objects["kyou/latest.json"]["Body"])
        manifest = json.loads(self.s3.objects[pointer["manifestPath"]]["Body"])
        self.assertEqual(pointer, result["pointer"])
        self.assertEqual(pointer["resourceVersion"], plan["manifest"]["resourceVersion"])
        self.assertEqual({row["name"] for row in manifest["files"]}, set(KYOU_FILES))
        for item in manifest["files"]:
            self.assertEqual(digest(self.s3.objects[item["path"]]["Body"]), item["sha256"])

    @patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker-phigros-data", "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/publisher", "GITHUB_WORKFLOW_REF": "example/publisher/.github/workflows/kyou.yml@refs/heads/master"})
    def test_kyou_refuses_pointer_update_outside_serialized_workflow(self):
        finalize(*self.kyou_plan("base", migration=True))
        original = self.s3.objects["kyou/latest.json"]["Body"]
        plan, receipts = self.kyou_plan("updated")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            with self.assertRaisesRegex(ValueError, "serialized master workflow"):
                finalize(plan, receipts)
        self.assertEqual(self.s3.objects["kyou/latest.json"]["Body"], original)

    @patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker-phigros-data", "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/publisher", "GITHUB_WORKFLOW_REF": "example/publisher/.github/workflows/kyou.yml@refs/heads/master"})
    def test_kyou_rechecks_baseline_after_manifest_write(self):
        finalize(*self.kyou_plan("base", migration=True))
        plan, receipts = self.kyou_plan("updated")
        winner = self.s3.objects["kyou/latest.json"]["Body"] + b" "
        complete = self.s3.complete_multipart_upload

        def concurrent(**args):
            result = complete(**args)
            if args["Key"].startswith("kyou/manifests/"):
                self.s3.seed("kyou/latest.json", winner)
            return result

        with patch.object(self.s3, "complete_multipart_upload", side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, "baseline changed"):
                finalize(plan, receipts)
        self.assertEqual(self.s3.objects["kyou/latest.json"]["Body"], winner)

    def test_rizline_preview_skip_requires_all_99_and_all_unindexed_after_replacement(self):
        from rizline_publisher.upstream import import_catalog
        for constants, indexed, success in [([99, 99], set(), True), ([99, 99], {"replacement"}, False), ([12, 99], set(), False)]:
            with self.subTest(constants=constants, indexed=indexed):
                official = {"levels": [{"id": "preview", "chartIds": ["preview.EZ", "preview.IN"], "musicId": "music", "illustrationId": "cover"},
                                       {"id": "released", "chartIds": ["released.IN"], "musicId": "music", "illustrationId": "cover"}],
                            "discOLevels": [], "musics": [{"id": "music", "musicName": "Song"}], "illustrations": [{"id": "cover"}],
                            "charts": [{"id": "preview.EZ", "level": "EZ", "difficulty": constants[0]}, {"id": "preview.IN", "level": "IN", "difficulty": constants[1]},
                                       {"id": "released.IN", "level": "IN", "difficulty": 12}],
                            "resourceReplacements": [{"withFeature": "pigeonCN", "pairs": [{"oldKey": "preview.EZ", "newKey": "replacement"}]}]}
                importer = Mock(config={"version": "2.7.1"}, version="v1")
                importer.default.return_value = official
                importer.addressables.bundles.side_effect = lambda key: ["bundle"] if key in indexed or key == "released.IN" else []
                def text(key):
                    if key.startswith("local."):
                        return b""
                    if key == "released.IN" or key in indexed:
                        return b'{"bPM":150,"lines":[]}'
                    raise ValueError("chart missing")
                importer.text.side_effect = text
                importer.cover.return_value = PNG
                importer.acb.return_value = sample_acb()
                path = self.root / "overrides.json"
                path.write_bytes(encode(overrides()))
                with patch("rizline_publisher.upstream.Importer", return_value=importer):
                    if success:
                        report = import_catalog(self.root / "work", self.root / "http", path, stats_url=None, log=lambda *args, **kwargs: None)
                        self.assertEqual(report["skippedPreviewSongs"][0]["songId"], "preview")
                        self.assertEqual(report["summary"]["songs"], 1)
                    else:
                        with self.assertRaisesRegex(ValueError, "official resources failed"):
                            import_catalog(self.root / "work", self.root / "http", path, stats_url=None, log=lambda *args, **kwargs: None)


if __name__ == "__main__":
    unittest.main()
