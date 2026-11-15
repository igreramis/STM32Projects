"""Download one locally approved S3 release. No GPIO, UART or flashing code."""

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


SOCKET_TIMEOUT = 5
TRANSFER_DEADLINE = 30
MAX_FIRMWARE_BYTES = 512 * 1024


class StageError(Exception):
    """Only fixed, non-secret error codes may cross the MQTT/log boundary."""


@dataclass(frozen=True)
class Release:
    target: str
    version: str
    size_bytes: int
    sha256: str
    bucket: str
    region: str
    key: str
    version_id: str

    @classmethod
    def load(cls, filename):
        release = cls(**json.loads(Path(filename).read_text()))
        if release.target != "NUCLEO-G0B1RE":
            raise ValueError("Unsupported configured firmware target")
        if type(release.size_bytes) is not int or not 8 <= release.size_bytes <= MAX_FIRMWARE_BYTES:
            raise ValueError("Invalid configured firmware size")
        if not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
            raise ValueError("Invalid configured firmware SHA-256")
        if release.bucket != "iot-lab-firmware-844274146091" or release.region != "ap-southeast-2":
            raise ValueError("Unsupported configured S3 bucket/region")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release.version):
            raise ValueError("Invalid configured release version")
        if release.key != f"firmware/stm32g0b1/{release.version}/firmware.bin":
            raise ValueError("Unexpected configured S3 key")
        if not isinstance(release.version_id, str) or not 1 <= len(release.version_id) <= 1024:
            raise ValueError("Missing configured S3 version ID")
        return release

    @property
    def host(self):
        return f"{self.bucket}.s3.{self.region}.amazonaws.com"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StageError("redirect_refused")


class FirmwareStager:
    def __init__(self, release, directory):
        self.release = release
        self.directory = Path(directory)
        # Use OS HTTPS CA roots, not the IoT-only CA file. No environment proxy,
        # HTTP downgrade or redirect may forward a presigned URL elsewhere.
        self.opener = build_opener(
            ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
        )

    def validate(self, command):
        if set(command) != {"command_id", "command", "version", "url"}:
            raise StageError("invalid_stage_fields")
        if command["command"] != "stage_firmware" or command["version"] != self.release.version:
            raise StageError("release_not_approved")
        url = command["url"]
        if not isinstance(url, str) or not 1 <= len(url) <= 3500:
            raise StageError("invalid_download_url")
        if any(ord(c) <= 32 or ord(c) >= 127 for c in url) or "\\" in url:
            raise StageError("invalid_download_url")
        try:
            parsed = urlsplit(url)
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=24)
            valid = (
                parsed.scheme == "https"
                and parsed.netloc == self.release.host
                and parsed.path == "/" + quote(self.release.key, safe="/")
                and not parsed.fragment
                and query.get("versionId") == [self.release.version_id]
                and query.get("X-Amz-Algorithm") == ["AWS4-HMAC-SHA256"]
                and all(len(values) == 1 for values in query.values())
                and re.fullmatch(r"[0-9a-f]{64}", query.get("X-Amz-Signature", [""])[0])
            )
        except ValueError:
            valid = False
        if not valid:
            raise StageError("url_not_approved")
        return url

    def stage(self, command):
        url = self.validate(command)
        temporary = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".download-", dir=self.directory)
            deadline = time.monotonic() + TRANSFER_DEADLINE
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as output:
                request = Request(url, headers={"Accept-Encoding": "identity"})
                with self.opener.open(request, timeout=SOCKET_TIMEOUT) as response:
                    if response.status != 200:
                        raise StageError("unexpected_http_status")
                    if response.headers.get("x-amz-version-id") != self.release.version_id:
                        raise StageError("s3_version_mismatch")
                    if response.headers.get("Content-Length") != str(self.release.size_bytes):
                        raise StageError("size_mismatch")
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise StageError("content_encoding_refused")
                    while True:
                        if time.monotonic() > deadline:
                            raise StageError("download_deadline_exceeded")
                        # read1 returns available data instead of waiting to fill
                        # a buffer; recheck elapsed time even on a trickle stream.
                        chunk = response.read1(min(16384, self.release.size_bytes - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.release.size_bytes:
                            raise StageError("size_mismatch")
                        digest.update(chunk)
                        output.write(chunk)
                if time.monotonic() > deadline:
                    raise StageError("download_deadline_exceeded")
                if total != self.release.size_bytes:
                    raise StageError("size_mismatch")
                if digest.hexdigest() != self.release.sha256:
                    raise StageError("sha256_mismatch")
                output.flush()
                os.fsync(output.fileno())
            # One fixed staging slot, never a remote-controlled destination.
            # The previous verified binary survives any download/check failure.
            destination = self.directory / "firmware.bin"
            os.replace(temporary, destination)
            temporary = None
            return {
                "version": self.release.version,
                "target": self.release.target,
                "size_bytes": total,
                "sha256": self.release.sha256,
                "s3_version_id": self.release.version_id,
                "staged_file": str(destination),
                "flashed": False,
            }
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise StageError(f"http_{status}") from None
        except (URLError, HTTPException, OSError):
            # Exception strings can contain the full signed URL. Never expose it.
            raise StageError("download_or_storage_failed") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)