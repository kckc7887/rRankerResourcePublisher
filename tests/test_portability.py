"""Cross-platform HTTP behavior used by the Ubuntu workflow, without network access."""
import http.client
import io
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rizline_publisher.upstream import Http, HttpError
from rizline_publisher.core import _windows_comparison_path


class PortabilityTests(unittest.TestCase):
    def test_extended_windows_drive_and_unc_spellings_preserve_containment(self):
        root = _windows_comparison_path(r"D:\work\release")
        self.assertTrue(_windows_comparison_path(r"\\?\D:\work\release\covers\hash.png").is_relative_to(root))
        self.assertFalse(_windows_comparison_path(r"\\?\D:\work\outside\hash.png").is_relative_to(root))
        share = _windows_comparison_path(r"\\server\share\release")
        self.assertTrue(_windows_comparison_path(r"\\?\UNC\server\share\release\covers\hash.png").is_relative_to(share))
        self.assertFalse(_windows_comparison_path(r"\\?\UNC\server\other\release\hash.png").is_relative_to(share))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_posix_auto_transport_needs_no_powershell_and_checks_certificates(self):
        # Patch this module's platform reference rather than changing pathlib's host platform.
        with patch("rizline_publisher.upstream.os", SimpleNamespace(name="posix")), patch("rizline_publisher.upstream.shutil.which") as which:
            client = Http(self.cache)
        self.assertEqual(client.transport, "urllib")
        which.assert_not_called()
        self.assertTrue(client.ssl_context.check_hostname)
        self.assertEqual(client.ssl_context.verify_mode, ssl.CERT_REQUIRED)

    def test_urllib_preserves_unicode_url_headers_binary_data_and_cache(self):
        client = Http(self.cache, transport="urllib")
        payload = b"\x00\xff\xfeUnityFS\x00"
        with patch("rizline_publisher.upstream.urllib.request.urlopen", return_value=io.BytesIO(payload)) as request:
            result = client.get("https://example.test/曲师/song.acb=abc123", {"game_id": "pigeongames.rizline"})
            self.assertEqual(result, payload)
            self.assertEqual(client.get("https://example.test/曲师/song.acb=abc123", {"game_id": "pigeongames.rizline"}), payload)
        request.assert_called_once()
        argument = request.call_args.args[0]
        self.assertIn("/%E6%9B%B2%E5%B8%88/", argument.full_url)
        self.assertEqual(argument.get_header("Game_id"), "pigeongames.rizline")
        self.assertIs(request.call_args.kwargs["context"], client.ssl_context)

    def test_truncated_response_retries_without_caching_partial_bytes(self):
        client = Http(self.cache, transport="urllib")

        class Truncated(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"partial", 100)

        with patch("rizline_publisher.upstream.urllib.request.urlopen", side_effect=[Truncated(), io.BytesIO(b"complete")]) as request, patch("rizline_publisher.upstream.time.sleep") as sleep:
            self.assertEqual(client.get("https://example.test/bundle"), b"complete")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual([file.read_bytes() for file in self.cache.glob("*.bin")], [b"complete"])

    def test_terminal_http_errors_do_not_retry_or_create_cache(self):
        client = Http(self.cache, transport="urllib")
        error = urllib.error.HTTPError("https://example.test/missing", 404, "missing", {}, None)
        with patch("rizline_publisher.upstream.urllib.request.urlopen", side_effect=error) as request, patch("rizline_publisher.upstream.time.sleep") as sleep:
            with self.assertRaises(HttpError) as caught:
                client.get("https://example.test/missing")
        self.assertEqual(caught.exception.status, 404)
        request.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_tls_failure_never_downgrades_to_plaintext_or_disables_checks(self):
        client = Http(self.cache, transport="urllib")
        with patch("rizline_publisher.upstream.urllib.request.urlopen", side_effect=ssl.SSLCertVerificationError("untrusted certificate")) as request, patch("rizline_publisher.upstream.time.sleep"):
            with self.assertRaises(ssl.SSLCertVerificationError):
                client.get("https://example.test/bundle")
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.args[0].full_url.startswith("https://") and call.kwargs["context"].verify_mode == ssl.CERT_REQUIRED for call in request.call_args_list))
        self.assertEqual(list(self.cache.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
