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
        catalog = {"schemaVersion": 1, "gameVersion": "2.7.1", "resourceVersion": "date", "songs": [
            {"id": "Song.A", "coverPath": cover, "audioPath": audio, "charts": [{"chartPath": chart}]}]}
        payloads = {chart: b"chart", audio: b"audio", cover: b"cover", prefix + "catalog.json": encode(catalog)}
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
