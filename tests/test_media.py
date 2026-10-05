import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from publisher.media import content_digest
from publisher.publication import finalize, prepare, upload_shard
from publisher.resources import local_candidate, normalize
from publisher.storage import digest, encode
from test_publication import S3


def png(level, changed=False):
    image = Image.new("RGBA", (16, 16), (10, 20, 30, 255))
    if changed:
        image.putpixel((0, 0), (11, 20, 30, 255))
    result = io.BytesIO()
    image.save(result, "PNG", compress_level=level)
    return result.getvalue()


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        original = root / "original.ogg"
        commands = {
            "original": ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "0.3", "-c:a", "libvorbis"],
            "remux": ["-i", str(original), "-c:a", "copy", "-fflags", "+bitexact"],
            "trim": ["-i", str(original), "-t", "0.2", "-c:a", "copy"],
            "gain": ["-i", str(original), "-c:a", "copy", "-metadata", "REPLAYGAIN_TRACK_GAIN=-5 dB"],
            "changed": ["-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100", "-t", "0.3", "-c:a", "libvorbis"],
        }
        cls.audio = {}
        for name, arguments in commands.items():
            path = root / f"{name}.ogg"
            subprocess.run(["ffmpeg", "-v", "error", *arguments, str(path)], check=True, timeout=30)
            cls.audio[name] = path.read_bytes()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.s3 = S3()
        for context in (patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker-phigros-data", "S3_ENDPOINT": "https://example.test",
                                               "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test"}),
                        patch("publisher.storage.boto3.client", return_value=self.s3)):
            context.start()
            self.addCleanup(context.stop)
        self.original = {"catalog.json": b'{"songs":[]}', "metadata/note_counts.tsv": b"notes",
                         "illustrations/song.png": png(0), "music/song.ogg": self.audio["original"]}
        assets = self.write_build(self.original)
        candidate = normalize("phigros", {"gameVersion": "4.0.1"}, assets, self.root / "metadata")
        self.publish(prepare("phigros", candidate, self.root / "plan", migration=True))
        self.s3.writes.clear()
        self.s3.reads.clear()

    def write_build(self, values):
        assets = []
        prefix = "phigros/releases/bundle/"
        for logical, body in values.items():
            path = self.root / prefix / logical
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            assets.append({"path": logical, "logical": logical, "original": prefix + logical,
                           "local": str(path), "sha256": digest(body), "size": len(body)})
        manifest = encode({"gameVersion": "4.0.1", "assets": assets})
        (self.root / prefix / "manifest.json").write_bytes(manifest)
        (self.root / "phigros/current.json").write_bytes(encode({"manifest": prefix + "manifest.json", "manifestSha256": digest(manifest)}))
        return assets

    def plan(self, values):
        self.write_build(values)
        candidate = local_candidate("phigros", self.root, self.root / "metadata")
        return prepare("phigros", candidate, self.root / "plan")

    def publish(self, plan):
        receipts = [upload_shard(plan, shard["id"], self.root / f"plan/shard-{shard['id']}",
                                self.root / f"receipt-{shard['id']}.json") for shard in plan["shards"]]
        return finalize(plan, receipts)

    def test_reencoded_media_keeps_old_bytes_and_next_run_reads_no_media(self):
        updated = {**self.original, "illustrations/song.png": png(9), "music/song.ogg": self.audio["remux"]}
        self.assertNotEqual(updated["illustrations/song.png"], self.original["illustrations/song.png"])
        self.assertNotEqual(updated["music/song.ogg"], self.original["music/song.ogg"])
        plan = self.plan(updated)
        self.assertEqual(plan["summary"]["upload"]["count"], 0)
        self.assertEqual(plan["summary"]["reuse"]["count"], 4)
        self.publish(plan)
        for asset in plan["manifest"]["assets"]:
            self.assertEqual(self.s3.objects[asset["objectKey"]]["Body"], self.original[asset["path"]])
        self.assertTrue(all("/manifests/" in key or key.endswith("/latest.json") for _, key, _ in self.s3.writes))
        self.s3.writes.clear()
        self.s3.reads.clear()
        result = self.publish(self.plan(updated))
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.s3.writes, [])
        self.assertTrue(all("/manifests/" in key or key.endswith("/latest.json") for key in self.s3.reads))

    def test_one_changed_pixel_uploads_only_that_image(self):
        changed = png(9, changed=True)
        plan = self.plan({**self.original, "illustrations/song.png": changed, "music/song.ogg": self.audio["remux"]})
        self.assertEqual(plan["summary"]["upload"], {"count": 1, "bytes": len(changed)})
        self.publish(plan)
        asset = next(row for row in plan["manifest"]["assets"] if row["path"] == "illustrations/song.png")
        self.assertEqual(self.s3.objects[asset["objectKey"]]["Body"], changed)

    def test_audio_change_uploads_only_that_song(self):
        changed = self.audio["changed"]
        plan = self.plan({**self.original, "illustrations/song.png": png(9), "music/song.ogg": changed})
        self.assertEqual(plan["summary"]["upload"], {"count": 1, "bytes": len(changed)})
        self.publish(plan)
        asset = next(row for row in plan["manifest"]["assets"] if row["path"] == "music/song.ogg")
        self.assertEqual(self.s3.objects[asset["objectKey"]]["Body"], changed)

    def test_audio_trim_and_playback_tags_are_not_reused(self):
        original = content_digest(self.audio["original"], ".ogg")
        for name in ("trim", "gain"):
            with self.subTest(name=name):
                self.assertNotEqual(content_digest(self.audio[name], ".ogg"), original)

    def test_corrupt_or_missing_baseline_media_never_falls_back_to_upload(self):
        key = next(key for key in self.s3.objects if key.startswith("phigros/illustrations/"))
        pointer = self.s3.objects["phigros/latest.json"]["Body"]
        self.s3.seed(key, b"!" + self.original["illustrations/song.png"][1:])
        with self.assertRaisesRegex(ValueError, "Baseline media corrupted"):
            self.plan({**self.original, "illustrations/song.png": png(9)})
        del self.s3.objects[key]
        from botocore.exceptions import ClientError
        with self.assertRaises(ClientError):
            self.plan({**self.original, "illustrations/song.png": png(9)})
        self.assertEqual(self.s3.writes, [])
        self.assertEqual(self.s3.objects["phigros/latest.json"]["Body"], pointer)

    def test_16_bit_pixel_changes_remain_distinct(self):
        images = []
        for value in (256, 257):
            image = Image.new("I;16", (1, 1), value)
            output = io.BytesIO()
            image.save(output, "PNG")
            images.append(output.getvalue())
        self.assertNotEqual(content_digest(images[0], ".png"), content_digest(images[1], ".png"))

    def test_truncated_audio_is_rejected(self):
        with self.assertRaises(ValueError):
            content_digest(self.audio["original"][:-20], ".ogg")


if __name__ == "__main__":
    unittest.main()
