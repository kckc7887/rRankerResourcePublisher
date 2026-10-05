import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from publisher.apk import ABI_NAMES, CERTIFICATE, publish, verify_apk
from publisher.storage import digest
from test_publication import S3


def apk_bytes(abi):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr(f"lib/{abi}/libmain.so", b"library")
    return data.getvalue()


class ApkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sdk/build-tools/36.0.0").mkdir(parents=True)
        self.s3 = S3()
        self.release = {"id": 123, "tag_name": "0.3.0", "draft": False, "prerelease": False, "published_at": "2026-09-02T00:00:00Z", "assets": []}
        for abi, name in ABI_NAMES.items():
            data = apk_bytes(abi)
            asset = {"name": f"rRanker-0.3.0-{name}.apk", "size": len(data), "state": "uploaded", "digest": f"sha256:{digest(data)}"}
            self.release["assets"].append(asset)
            self.s3.seed(f"release/0.3.0/{asset['name']}", data)
        for context in (
            patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker", "S3_ENDPOINT": "https://example.test", "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test", "ANDROID_HOME": str(self.root / "sdk")}),
            patch("publisher.storage.boto3.client", return_value=self.s3),
            patch("publisher.apk.github", side_effect=lambda path: copy.deepcopy(self.release)),
            patch("publisher.apk.subprocess.check_output", side_effect=self.tool_output),
        ):
            context.start()
            self.addCleanup(context.stop)

    def tool_output(self, args, **kwargs):
        if "badging" in args:
            return "package: name='com.rranker.app' versionName='0.3.0' versionCode='3'\n"
        return f"Verifies\nSigner #1 certificate SHA-256 digest: {CERTIFICATE}\n"

    def test_bootstrap_copies_only_four_fixed_apks_after_verification(self):
        old = copy.deepcopy(self.s3.objects)
        self.s3.seed("assets/image.png", b"protected")
        result = publish(123, self.root, bootstrap=True, execute=True)
        self.assertEqual(result["completed"], [f"release/rRanker-{name}.apk" for name in ABI_NAMES.values()])
        self.assertTrue(all(mode == "copy" for mode, _, _ in self.s3.writes))
        for key, row in old.items():
            self.assertEqual(self.s3.objects[key], row)
        self.assertEqual(self.s3.objects["assets/image.png"]["Body"], b"protected")
        self.s3.writes.clear()
        publish(123, self.root, bootstrap=True, execute=True)
        self.assertEqual(self.s3.writes, [])

    def test_invalid_fourth_apk_prevents_all_fixed_address_writes(self):
        self.release["assets"][-1]["size"] += 1
        with self.assertRaisesRegex(ValueError, "checksum/size"):
            publish(123, self.root, bootstrap=True, execute=True)
        self.assertEqual(self.s3.writes, [])
        self.assertEqual(json.loads((self.root / "report.json").read_bytes())["completed"], [])

    def test_failed_copy_reports_completed_objects_and_retry_finishes(self):
        self.s3.fail_key = "release/rRanker-x86.apk"
        with self.assertRaises(Exception):
            publish(123, self.root, bootstrap=True, execute=True)
        result = json.loads((self.root / "report.json").read_bytes())
        self.assertEqual(result["completed"], ["release/rRanker-arm64.apk", "release/rRanker-armeabi.apk"])
        self.s3.fail_key = None
        result = publish(123, self.root, bootstrap=True, execute=True)
        self.assertEqual(len(result["completed"]), 4)

    def test_rejects_draft_prerelease_and_delayed_old_release(self):
        for field in ("draft", "prerelease"):
            self.release[field] = True
            with self.assertRaisesRegex(ValueError, "formal release"):
                publish(123, self.root, bootstrap=True, execute=True)
            report = json.loads((self.root / "report.json").read_bytes())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["completed"], [])
            self.release[field] = False
        with patch("publisher.apk.github", side_effect=[self.release, {**self.release, "id": 999}]):
            with self.assertRaisesRegex(ValueError, "older Release"):
                publish(123, self.root, bootstrap=True, execute=True)
        self.assertEqual(self.s3.writes, [])

    def test_wrong_abi_or_signer_is_rejected(self):
        path = self.root / "fixture.apk"
        path.write_bytes(apk_bytes("x86"))
        with self.assertRaisesRegex(ValueError, "ABI mismatch"):
            verify_apk(path, "arm64-v8a", "0.3.0")
        with patch("publisher.apk.subprocess.check_output", side_effect=[self.tool_output(["badging"]), "Signer #1 certificate SHA-256 digest: " + "a" * 64]):
            with self.assertRaisesRegex(ValueError, "signing certificate"):
                verify_apk(path, "x86", "0.3.0")


if __name__ == "__main__":
    unittest.main()
