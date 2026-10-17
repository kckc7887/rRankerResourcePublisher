import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import requests
from botocore.exceptions import ClientError

from publisher.__main__ import main
from publisher.publication import prepare
from publisher.resources import normalize
from publisher.storage import digest, encode
from publisher.yunxiao import REPOSITORY, dispatch, source_workflow, upload
from test_publication import S3, sftp_mock

SHA = "a" * 40


def response(body, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = body if isinstance(body, bytes) else encode(body)
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class YunxiaoDispatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": "refs/heads/master", "GITHUB_SHA": SHA,
            "GITHUB_RUN_ID": "123", "YUNXIAO_ENDPOINT": "https://instance.devops.aliyuncs.com",
            "YUNXIAO_APK_PIPELINE_ID": "456", "YUNXIAO_PHIGROS_PIPELINE_ID": "789",
            "YUNXIAO_TOKEN": "private-flow-token", "S3_SECRET_ACCESS_KEY": "private-storage-key",
        }))
        self.enterContext(patch("publisher.yunxiao.time.sleep"))

    def test_release_dispatch_waits_for_success_and_sends_only_public_run_parameters(self):
        submitted = {}
        states = iter(("WAITING", "RUNNING", "SUCCESS"))
        def start(url, *, json, headers, timeout):
            submitted.update(json)
            self.assertEqual(headers["x-yunxiao-token"], "private-flow-token")
            return response(9)
        with patch("publisher.yunxiao.requests.post", side_effect=start), \
                patch("publisher.yunxiao.requests.get", side_effect=lambda *a, **k: response({"status": next(states)})):
            self.assertEqual(dispatch("apk", release_id=415), 9)
        self.assertEqual(json.loads(submitted["params"])["envs"], {
            "GAME": "apk", "PUBLISHER_SHA": SHA, "SOURCE_RUN_ID": "123", "RELEASE_ID": "415",
        })
        self.assertNotIn("private-storage-key", str(submitted))

    def test_failed_or_cancelled_cloud_publications_do_not_report_success(self):
        for status in ("FAIL", "CANCELED"):
            with self.subTest(status=status), patch("publisher.yunxiao.requests.post", return_value=response(9)), \
                    patch("publisher.yunxiao.requests.get", return_value=response({"status": status})):
                with self.assertRaisesRegex(RuntimeError, status):
                    dispatch("kyou", attempt=1)

    def test_timeout_stops_the_cloud_writer(self):
        stopped = []
        with patch("publisher.yunxiao.requests.post", return_value=response(9)), \
                patch("publisher.yunxiao.requests.get", return_value=response({"status": "RUNNING"})), \
                patch("publisher.yunxiao.requests.put", side_effect=lambda url, **k: stopped.append(url) or response(True)), \
                patch("publisher.yunxiao.time.monotonic", side_effect=(0, 0, 100)):
            with self.assertRaises(TimeoutError):
                dispatch("kyou", attempt=1, timeout=10)
        self.assertEqual(stopped, ["https://instance.devops.aliyuncs.com/oapi/v1/flow/pipelines/789/runs/9"])

    def test_github_cancellation_stops_the_cloud_writer(self):
        stopped = []
        with patch("publisher.yunxiao.requests.post", return_value=response(9)), \
                patch("publisher.yunxiao.requests.get", side_effect=KeyboardInterrupt), \
                patch("publisher.yunxiao.requests.put", side_effect=lambda url, **k: stopped.append(url) or response(True)):
            with self.assertRaises(KeyboardInterrupt):
                dispatch("kyou", attempt=1)
        self.assertTrue(stopped)

    def test_non_master_source_is_rejected(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/other"}):
            with self.assertRaisesRegex(ValueError, "master workflow"):
                dispatch("apk", release_id=415)

    def test_cloud_apk_source_check_rejects_a_cancelled_github_dispatch(self):
        run = {"head_branch": "master", "head_sha": SHA, "path": ".github/workflows/apk.yml",
               "event": "workflow_dispatch", "status": "completed"}
        with patch("publisher.yunxiao.requests.get", return_value=response(run)):
            with self.assertRaisesRegex(ValueError, "active master"):
                source_workflow("apk", 123, SHA)


class YunxiaoUploadTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.s3 = S3()
        self.ssh = self.enterContext(sftp_mock(self.s3))
        self.enterContext(patch("publisher.storage.boto3.client", return_value=self.s3))
        self.enterContext(patch("publisher.storage.time.sleep"))
        self.enterContext(patch.dict(os.environ, {
            "S3_BUCKET_NAME": "rranker-phigros-data", "S3_ENDPOINT": "https://storage.example",
            "S3_ACCESS_KEY_ID": "access", "S3_SECRET_ACCESS_KEY": "secret",
            "GITHUB_ARTIFACT_TOKEN": "private-artifact-token", "PUBLISHER_SHA": SHA,
        }))
        objects = []
        for logical, data in {"catalog.json": b'{"songs":[]}', "metadata/note_counts.tsv": b"notes"}.items():
            path = self.root / "input" / logical
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            objects.append({"logical": logical, "original": logical, "size": len(data), "sha256": digest(data), "local": str(path)})
        candidate = normalize("phigros", {"gameVersion": "4.0.1"}, objects, self.root / "metadata")
        self.prepared = self.root / "prepared"
        self.plan = prepare("phigros", candidate, self.prepared, migration=True)
        self.run = {"head_branch": "master", "head_sha": SHA, "path": ".github/workflows/phigros.yml",
                    "event": "workflow_dispatch", "status": "in_progress", "run_attempt": 1}
        self.artifacts = []
        self.payloads = {}
        self.add_artifact("phigros-plan-1", {"plan.json": encode(self.plan)})
        for shard in self.plan["shards"]:
            directory = self.prepared / f"shard-{shard['id']}"
            self.add_artifact(f"phigros-shard-{shard['id']}-1", {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*") if path.is_file()})
        self.enterContext(patch("publisher.yunxiao.requests.get", side_effect=self.get))

    def add_artifact(self, name, files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, body in files.items():
                archive.writestr(path, body)
        artifact_id = len(self.artifacts) + 1
        body = buffer.getvalue()
        self.artifacts.append({"id": artifact_id, "name": name, "expired": False, "digest": f"sha256:{digest(body)}", "size_in_bytes": len(body)})
        self.payloads[artifact_id] = body

    def get(self, url, **kwargs):
        if url.startswith("https://artifacts.example/"):
            self.assertNotIn("headers", kwargs)
            return response(self.payloads[int(url.rsplit("/", 1)[1])])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer private-artifact-token")
        if url.endswith("/actions/runs/123"):
            return response(copy.deepcopy(self.run))
        if "/artifacts?" in url:
            return response({"artifacts": copy.deepcopy(self.artifacts)})
        artifact_id = int(url.rsplit("/", 2)[1])
        return response(b"", 302, {"Location": f"https://artifacts.example/{artifact_id}"})

    def test_cloud_finishes_the_original_plan_and_preserves_unowned_objects(self):
        self.s3.seed("fonts/protected.woff", b"protected")
        first = self.plan["tasks"][0]
        body = (self.prepared / f"shard-{first['shard']}" / first["key"]).read_bytes()
        self.s3.seed(first["key"], body)
        result = upload("phigros", 123, 1, self.root / "cloud")
        pointer = json.loads(self.s3.objects["phigros/latest.json"]["Body"])
        self.assertEqual(pointer, result["pointer"])
        self.assertEqual(pointer["resourceVersion"], self.plan["manifest"]["resourceVersion"])
        self.assertEqual(self.s3.objects["fonts/protected.woff"]["Body"], b"protected")
        for item in self.plan["tasks"]:
            self.assertEqual(digest(self.s3.objects[item["key"]]["Body"]), item["sha256"])

    def test_artifact_checksum_failure_prevents_storage_writes(self):
        self.payloads[1] += b"changed"
        with self.assertRaisesRegex(ValueError, "artifact checksum"):
            upload("phigros", 123, 1, self.root / "cloud")
        self.assertEqual(self.s3.writes, [])

    def test_other_branch_or_commit_cannot_publish(self):
        for field, value in (("head_branch", "other"), ("head_sha", "b" * 40), ("path", ".github/workflows/kyou.yml")):
            original = self.run[field]
            self.run[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "active master"):
                upload("phigros", 123, 1, self.root / "cloud")
            self.run[field] = original
        self.assertEqual(self.s3.writes, [])

    def test_cancelled_github_run_never_switches_the_pointer(self):
        original_upload = self.ssh.open_sftp.return_value.__enter__.return_value.putfo.side_effect
        def cancel_after_transfer(*args, **kwargs):
            original_upload(*args, **kwargs)
            self.run["status"] = "completed"
        self.ssh.open_sftp.return_value.__enter__.return_value.putfo.side_effect = cancel_after_transfer
        with self.assertRaisesRegex(ValueError, "active master"):
            upload("phigros", 123, 1, self.root / "cloud")
        self.assertNotIn("phigros/latest.json", self.s3.objects)


class UploadFailureClassificationTests(unittest.TestCase):
    def test_storage_failure_requests_fallback_but_conflict_and_corruption_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.json"
            plan.write_text("{}", encoding="utf-8")
            for error, expected in ((ClientError({"ResponseMetadata": {"HTTPStatusCode": 503}, "Error": {}}, "PutObject"), True),
                                    (ClientError({"ResponseMetadata": {"HTTPStatusCode": 412}, "Error": {}}, "PutObject"), False),
                                    (ValueError("Shard payload corrupted"), False)):
                output = root / "outputs"
                output.unlink(missing_ok=True)
                with self.subTest(error=error), patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
                        patch("sys.argv", ["publisher", "shard", "--plan", str(plan), "--shard", "0", "--payload", str(root), "--receipt", str(root / "receipt.json")]), \
                        patch("publisher.__main__.upload_shard", side_effect=error):
                    with self.assertRaises(type(error)):
                        main()
                self.assertEqual(output.exists(), expected)
                if expected:
                    self.assertEqual(output.read_text(encoding="utf-8"), "cloud_fallback=true\n")


if __name__ == "__main__":
    unittest.main()
