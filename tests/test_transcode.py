import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rizline_publisher.transcode import is_m4a, transcode_acb
from test_rizline import SAMPLE_M4A, sample_acb


class TranscodeTests(unittest.TestCase):
    def test_is_m4a_requires_ftyp_brand(self):
        self.assertTrue(is_m4a(SAMPLE_M4A))
        self.assertFalse(is_m4a(sample_acb()))
        self.assertFalse(is_m4a(b"\x00\x00\x00\x18ftypXXXX" + bytes(16)))
        self.assertFalse(is_m4a(b""))

    def test_transcode_reuses_cache_and_skips_tools(self):
        acb = sample_acb()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / (hashlib.sha256(acb).hexdigest() + ".m4a")).write_bytes(SAMPLE_M4A)
            with patch("rizline_publisher.transcode.vgmstream_cli", side_effect=AssertionError("should not decode")):
                self.assertEqual(transcode_acb(acb, cache, 1720 / 44100), SAMPLE_M4A)

    def test_missing_tools_fail_before_writing_release_audio(self):
        with tempfile.TemporaryDirectory() as directory, patch("rizline_publisher.transcode.shutil.which", return_value=None), patch.dict("os.environ", {"VGMSTREAM_CLI": "", "FFMPEG": "", "FFPROBE": ""}, clear=False):
            with self.assertRaisesRegex(ValueError, "vgmstream-cli"):
                transcode_acb(sample_acb(), directory, 0.039)

    def test_duration_mismatch_is_rejected(self):
        acb = sample_acb()
        with tempfile.TemporaryDirectory() as directory:
            def fake_run(args, timeout):
                completed = type("Completed", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
                if args[0] == "vgmstream-cli":
                    Path(args[args.index("-o") + 1]).write_bytes(b"RIFF" + bytes(40))
                elif args[0] == "ffmpeg":
                    Path(args[-1]).write_bytes(SAMPLE_M4A)
                else:
                    completed.stdout = b"9.0\n"
                return completed
            with patch("rizline_publisher.transcode.vgmstream_cli", return_value="vgmstream-cli"), \
                    patch("rizline_publisher.transcode.ffmpeg_cli", return_value="ffmpeg"), \
                    patch("rizline_publisher.transcode.ffprobe_cli", return_value="ffprobe"), \
                    patch("rizline_publisher.transcode._run", side_effect=fake_run):
                with self.assertRaisesRegex(ValueError, "Transcoded duration"):
                    transcode_acb(acb, directory, 0.039)


if __name__ == "__main__":
    unittest.main()
