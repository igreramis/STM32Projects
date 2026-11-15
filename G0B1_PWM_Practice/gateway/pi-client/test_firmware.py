from dataclasses import replace
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from client import CommandHandler
from firmware import FirmwareStager, NoRedirect, Release, StageError


DATA = b"test-only-binary-not-for-flashing"


class Response(io.BytesIO):
    def __init__(self, data, release, status=200, **headers):
        super().__init__(data)
        self.status = status
        self.headers = {
            "Content-Length": str(release.size_bytes),
            "x-amz-version-id": release.version_id,
            **headers,
        }


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        approved = Release.load(Path(__file__).with_name("release.json"))
        self.release = replace(approved, size_bytes=len(DATA), sha256=hashlib.sha256(DATA).hexdigest())
        self.stager = FirmwareStager(self.release, Path(self.temp.name) / "staged")
        self.stager.opener = Mock()
        self.query = {
            "versionId": self.release.version_id,
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Signature": "a" * 64,
        }
        self.url = f"https://{self.release.host}/{self.release.key}?{urlencode(self.query)}"
        self.command = {"command_id": "stage-001", "command": "stage_firmware",
                        "version": self.release.version, "url": self.url}

    def respond(self, data=DATA, **kwargs):
        self.stager.opener.open.return_value = Response(data, self.release, **kwargs)

    def test_pinned_release_metadata(self):
        release = Release.load(Path(__file__).with_name("release.json"))
        self.assertEqual(release.version_id, "vtuJfI9yuJtFx6EqWSkSpULFENCCgo54")
        self.assertEqual(release.size_bytes, 19212)

    def test_valid_url(self):
        self.assertEqual(self.stager.validate(self.command), self.url)

    def test_unapproved_urls_rejected_before_network(self):
        urls = [
            self.url.replace("https:", "http:"),
            self.url.replace(self.release.host, "127.0.0.1"),
            self.url.replace(self.release.host, "169.254.169.254"),
            self.url.replace(self.release.host, self.release.host + ".example.com"),
            self.url.replace(self.release.host, "user@" + self.release.host),
            self.url.replace(self.release.host, self.release.host + ":443"),
            self.url.replace("firmware.bin", "../firmware.bin"),
            self.url.replace(self.release.version_id, "wrong-version"),
            self.url + "#fragment",
            self.url + "&versionId=duplicate",
            self.url + "&X-Amz-Signature=duplicate",
            self.url.replace("X-Amz-Signature=", "other="),
            self.url + "\n",
            self.url + "\\",
            "x" * 3501,
            None,
        ]
        for url in urls:
            with self.subTest(url_index=urls.index(url)):
                with self.assertRaises(StageError):
                    self.stager.stage(dict(self.command, url=url))
        self.stager.opener.open.assert_not_called()

    def test_unapproved_fields_and_version(self):
        for request in (dict(self.command, version="9.0.0"),
                        dict(self.command, target="other"),
                        dict(self.command, destination="/tmp/unsafe"),
                        dict(self.command, command="flash_firmware")):
            with self.assertRaises(StageError):
                self.stager.validate(request)

    def test_verified_staging_atomic_and_private(self):
        self.respond()
        result = self.stager.stage(self.command)
        destination = Path(result["staged_file"])
        self.assertEqual(destination.read_bytes(), DATA)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["s3_version_id"], self.release.version_id)
        self.assertFalse(result["flashed"])
        self.assertEqual(list(self.stager.directory.iterdir()), [destination])
        self.assertNotIn(self.url, json.dumps(result))
        request = self.stager.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Accept-encoding"), "identity")

    def test_failure_preserves_previous_binary_and_cleans_partial(self):
        self.stager.directory.mkdir()
        previous = self.stager.directory / "firmware.bin"
        previous.write_bytes(b"previous-verified")
        self.respond(b"x" * len(DATA))
        with self.assertRaisesRegex(StageError, "sha256_mismatch"):
            self.stager.stage(self.command)
        self.assertEqual(previous.read_bytes(), b"previous-verified")
        self.assertEqual(list(self.stager.directory.iterdir()), [previous])

    def test_truncated_and_oversized_body(self):
        for data in (DATA[:-1], DATA + b"x"):
            self.respond(data)
            with self.assertRaisesRegex(StageError, "size_mismatch"):
                self.stager.stage(self.command)
            self.assertEqual(list(self.stager.directory.iterdir()), [])

    def test_header_failures(self):
        cases = [
            ({"Content-Length": "999999999"}, "size_mismatch"),
            ({"Content-Length": None}, "size_mismatch"),
            ({"x-amz-version-id": "wrong"}, "s3_version_mismatch"),
            ({"Content-Encoding": "gzip"}, "content_encoding_refused"),
            ({"status": 206}, "unexpected_http_status"),
        ]
        for headers, error in cases:
            with self.subTest(error=error):
                self.respond(**headers)
                with self.assertRaisesRegex(StageError, error):
                    self.stager.stage(self.command)
                self.assertEqual(list(self.stager.directory.iterdir()), [])

    def test_deadline(self):
        self.respond()
        with patch("firmware.time.monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(StageError, "download_deadline_exceeded"):
                self.stager.stage(self.command)

    def test_network_errors_are_redacted(self):
        for error in (URLError(self.url), TimeoutError(self.url), IncompleteRead(b"", 20)):
            self.stager.opener.open.side_effect = error
            with self.assertRaises(StageError) as caught:
                self.stager.stage(self.command)
            self.assertEqual(str(caught.exception), "download_or_storage_failed")
            self.assertEqual(list(self.stager.directory.iterdir()), [])

    def test_http_403_is_redacted(self):
        self.stager.opener.open.side_effect = HTTPError(self.url, 403, self.url, {}, io.BytesIO())
        with self.assertRaisesRegex(StageError, "^http_403$"):
            self.stager.stage(self.command)

    def test_redirects_are_refused(self):
        with self.assertRaisesRegex(StageError, "redirect_refused"):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example")

    def test_storage_error(self):
        self.respond()
        with patch("firmware.os.replace", side_effect=OSError("test disk error")):
            with self.assertRaisesRegex(StageError, "download_or_storage_failed"):
                self.stager.stage(self.command)
        self.assertEqual(list(self.stager.directory.iterdir()), [])

    def test_mqtt_progress_result_and_duplicate(self):
        self.respond()
        handler = CommandHandler("pi-lab-01", stager=self.stager)
        progress = Mock()
        payload = json.dumps(self.command).encode()
        response = handler.handle(payload, 12, notify=progress)
        self.assertEqual(progress.call_args.args[0]["status"], "downloading")
        self.assertEqual(response["status"], "verified")
        self.assertEqual(response["command_id"], self.command["command_id"])
        self.assertFalse(response["flashed"])
        self.assertEqual(handler.handle(payload, 13, notify=progress), response)
        self.stager.opener.open.assert_called_once()
        progress.assert_called_once()
        status = handler.handle(b'{"command_id":"status-1","command":"get_status"}', 14)
        self.assertEqual(status["data"]["firmware_stage"]["status"], "verified")
        self.assertNotIn(self.url, repr(handler.cache))

    def test_same_id_changed_url_conflicts(self):
        self.respond()
        handler = CommandHandler("pi-lab-01", stager=self.stager)
        handler.handle(json.dumps(self.command).encode(), 0)
        changed = dict(self.command, url=self.url + "&other=value")
        response = handler.handle(json.dumps(changed).encode(), 0)
        self.assertEqual(response["error"], "command_id_reused")
        self.stager.opener.open.assert_called_once()

    def test_download_failure_ack_and_status(self):
        self.stager.opener.open.side_effect = URLError(self.url)
        handler = CommandHandler("pi-lab-01", stager=self.stager)
        response = handler.handle(json.dumps(self.command).encode(), 0)
        self.assertEqual(response["status"], "error")
        self.assertEqual(handler.stage_status["status"], "error")
        self.assertFalse(response["flashed"])
        self.assertNotIn(self.url, json.dumps(response))

    def test_rejected_command_does_not_download(self):
        handler = CommandHandler("pi-lab-01", stager=self.stager)
        progress = Mock()
        request = dict(self.command, version="2.0.0")
        response = handler.handle(json.dumps(request).encode(), 0, notify=progress)
        self.assertEqual(response["error"], "release_not_approved")
        progress.assert_not_called()
        self.stager.opener.open.assert_not_called()

    def test_publish_failure_does_not_start_download(self):
        handler = CommandHandler("pi-lab-01", stager=self.stager)
        with self.assertRaises(TimeoutError):
            handler.handle(json.dumps(self.command).encode(), 0,
                           notify=Mock(side_effect=TimeoutError()))
        self.stager.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()