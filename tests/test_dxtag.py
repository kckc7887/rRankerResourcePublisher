import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

from publisher.__main__ import main
from publisher.dxtag import SONG_LIST_URL
from test_publication import S3

AXES = ("键盘", "星星", "技巧", "体力", "爆发")
NAMES = ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")


class Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        return json.loads(self.content)


def engine(difficulties, scores=None):
    scores = [1.2, 3.4, 5.6, 7.8, 9.0] if scores is None else scores
    return [{"title": "Song", "difficulty": NAMES[item], "scores": dict(zip(AXES, scores, strict=True))} for item in difficulties]


class DxtagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s3 = S3()
        self.urls = []
        self.absent = set()
        self.chart_bytes = {}
        self.stdout = {}
        self.catalog = {"songs": [
            {"id": 8, "difficulties": {"standard": [{}], "dx": []}},
            {"id": 30, "difficulties": {"standard": [{}], "dx": [{}]}},
            {"id": 1639, "difficulties": {"standard": [], "dx": [{}]}},
            {"id": 100018, "difficulties": {"standard": [{}], "dx": [{}], "utage": [{}]}},
        ]}
        for context in (
            patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker-maimai-data", "S3_ENDPOINT": "https://s3.example", "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test", "DXTAG_ROOT": "DXTag"}),
            patch("publisher.storage.boto3.client", return_value=self.s3),
            patch("publisher.dxtag.requests.get", side_effect=self.fetch),
            patch("publisher.dxtag.subprocess.run", side_effect=self.score),
        ):
            context.start()
            self.addCleanup(context.stop)

    def fetch(self, url, timeout):
        self.urls.append(url)
        if url == SONG_LIST_URL:
            return Response(self.catalog)
        chart_id = int(url.rsplit("/", 1)[1].removesuffix(".txt"))
        if chart_id in self.absent:
            return Response(b"", status=404)
        return Response(self.chart_bytes.get(chart_id, b"chart"))

    def score(self, args, **kwargs):
        chart = Path(args[-1]).read_bytes()
        if chart == b"fail":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="MASTER：失败")
        if chart in self.stdout:
            return subprocess.CompletedProcess(args, 0, stdout=self.stdout[chart], stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(engine([3])), stderr="")

    def publish(self, *args):
        stdout = io.StringIO()
        with patch("sys.argv", ["publisher", "dxtag", "--output", str(self.root), *args]), redirect_stdout(stdout):
            try:
                main()
            except SystemExit as error:
                code = error.code or 0
            else:
                code = 0
        text = stdout.getvalue()
        return code, json.loads(text) if text else None

    def test_lists_missing_standard_and_dx_charts_without_downloading(self):
        self.s3.seed("DXTag/8.json", b"keep")
        self.s3.seed("DXTag/extra.txt", b"extra")
        code, result = self.publish()
        self.assertEqual(code, 0)
        self.assertEqual(result["missing"], [30, 10030, 11639])
        self.assertEqual(result["uploaded"], [])
        self.assertEqual(self.urls, [SONG_LIST_URL])
        self.assertEqual(self.s3.writes, [])
        self.assertEqual(self.s3.objects["DXTag/8.json"]["Body"], b"keep")
        self.assertEqual(self.s3.reads, [])

    def test_leading_zero_object_does_not_satisfy_chart_id(self):
        self.s3.seed("DXTag/08.json", b"alias")
        code, result = self.publish()
        self.assertEqual(code, 0)
        self.assertIn(8, result["missing"])

    def test_uploads_difficulty_ids_and_one_decimal_axis_arrays(self):
        self.catalog = {"songs": [{"id": 30, "difficulties": {"standard": [], "dx": [{}]}}]}
        self.chart_bytes[10030] = b"ordered"
        self.stdout[b"ordered"] = json.dumps(engine([4, 0], [0, 10, 1, 1.5, 9]))
        code, result = self.publish("--execute")
        self.assertEqual(code, 0)
        self.assertEqual(result["uploaded"], [10030])
        self.assertEqual(self.s3.objects["DXTag/10030.json"]["Body"],
                         b'[{"difficulty":0,"scores":[0.0,10.0,1.0,1.5,9.0]},{"difficulty":4,"scores":[0.0,10.0,1.0,1.5,9.0]}]\n')
        self.assertEqual(self.s3.reads, [])

    def test_existing_object_is_left_unchanged(self):
        self.catalog = {"songs": [{"id": 30, "difficulties": {"standard": [{}], "dx": [{}]}}]}
        self.s3.seed("DXTag/30.json", b"keep")
        code, result = self.publish("--execute")
        self.assertEqual(code, 0)
        self.assertEqual(result["uploaded"], [10030])
        self.assertEqual(self.s3.objects["DXTag/30.json"]["Body"], b"keep")
        self.assertEqual([key for mode, key, _size in self.s3.writes], ["DXTag/10030.json"])

    def test_precondition_failure_does_not_replace_bytes(self):
        self.catalog = {"songs": [{"id": 8, "difficulties": {"standard": [{}], "dx": []}}]}
        self.s3.seed("DXTag/8.json", b"keep")
        self.s3.paginate = lambda Bucket, Prefix: [{"Contents": []}]
        code, result = self.publish("--execute")
        self.assertEqual(code, 0)
        self.assertEqual(result["uploaded"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(self.s3.objects["DXTag/8.json"]["Body"], b"keep")

    def test_failed_chart_keeps_successful_upload_and_exits(self):
        self.catalog = {"songs": [{"id": 30, "difficulties": {"standard": [{}], "dx": [{}]}}]}
        self.chart_bytes[30] = b"fail"
        code, result = self.publish("--execute")
        self.assertEqual(code, 1)
        self.assertEqual(result["uploaded"], [10030])
        self.assertEqual([row["id"] for row in result["failed"]], [30])
        self.assertNotIn("DXTag/30.json", self.s3.objects)
        self.assertEqual(self.s3.objects["DXTag/10030.json"]["Body"], b'[{"difficulty":3,"scores":[1.2,3.4,5.6,7.8,9.0]}]\n')

    def test_missing_chart_download_is_not_uploaded(self):
        self.catalog = {"songs": [{"id": 8, "difficulties": {"standard": [{}], "dx": []}}]}
        self.absent.add(8)
        code, result = self.publish("--execute")
        self.assertEqual(code, 1)
        self.assertEqual(result["uploaded"], [])
        self.assertEqual(self.s3.writes, [])

    def test_unsupported_song_id_writes_nothing(self):
        self.catalog = {"songs": [{"id": 20000, "difficulties": {"standard": [{}], "dx": []}}]}
        with self.assertRaisesRegex(ValueError, "20000"):
            self.publish("--execute")
        self.assertEqual(self.s3.writes, [])

    def test_wrong_bucket_is_rejected(self):
        with patch.dict(os.environ, {"S3_BUCKET_NAME": "rranker"}):
            with self.assertRaisesRegex(ValueError, "Wrong bucket"):
                self.publish()
        self.assertEqual(self.urls, [])

    def test_invalid_score_shape_is_not_uploaded(self):
        self.catalog = {"songs": [{"id": 8, "difficulties": {"standard": [{}], "dx": []}}]}
        for body in (
            [{"title": "Song", "difficulty": "MASTER", "scores": {"键盘": 1.2}}],
            [{"title": "Song", "difficulty": "MASTER", "scores": dict(zip(AXES, [1.2, 3.4, 5.6, 7.8, 10.1], strict=True))}],
            [{"title": "Song", "difficulty": "MASTER", "scores": dict(zip(AXES, [1.2, 3.4, 5.6, 7.8, 9.0], strict=True))}] * 2,
        ):
            with self.subTest(body=body):
                self.s3.objects.clear()
                self.s3.writes.clear()
                self.stdout[b"chart"] = json.dumps(body)
                code, result = self.publish("--execute")
                self.assertEqual(code, 1)
                self.assertEqual(result["uploaded"], [])
                self.assertEqual(self.s3.writes, [])
