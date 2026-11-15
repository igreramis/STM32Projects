"""Durable predecessor pin; never learn the initial state from the target UART.

The caller holds OtaJobs' lifetime flock. An intent is committed BEFORE erase;
only a complete engine readback may replace it. Missing/corrupt/pending state on
subsequent runs requires operator investigation, not automatic adoption/retry.
"""

import json
import os
from pathlib import Path
import re
import tempfile

import rom_local
from signed_release import canonical, unique, version


class VerifiedState:
    def __init__(self, root, seed_version):
        self.root = Path(root)
        self.seed_version = seed_version
        self.path = self.root / "predecessor.json"
        self.marker = self.root.parent / ("." + self.root.name + "-initialized")
        self.healthy = True

    def _mark_initialized(self):
        if self.marker.is_symlink():
            raise ValueError("predecessor_storage_failed")
        if self.marker.exists():
            if not self.marker.is_file() or self.marker.stat().st_size != 0:
                raise ValueError("predecessor_storage_failed")
            return
        try:
            fd = os.open(str(self.marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            rom_local.engine.RunRecord._sync_dir(self.root.parent)
        except BaseException:
            self.healthy = False
            raise

    def _write(self, value):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".state-", dir=str(self.root))
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(self.path))
            rom_local.engine.RunRecord._sync_dir(self.root)
        except BaseException:
            self.healthy = False
            raise
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def load(self, releases):
        if not self.healthy:
            raise ValueError("predecessor_storage_failed")
        if self.root.is_symlink():
            raise ValueError("predecessor_storage_failed")
        if not self.root.exists():
            # A separate lifetime marker also detects loss of the state directory.
            # Commit it BEFORE seeding; any interruption then fails closed.
            if self.marker.exists() or self.marker.is_symlink():
                raise ValueError("predecessor_missing")
            self._mark_initialized()
            self.root.mkdir(mode=0o700)
            rom_local.engine.RunRecord._sync_dir(self.root.parent)
            if self.seed_version == "original":
                digest = rom_local.LEGACY_BASELINE_SHA256
            else:
                digest = releases[self.seed_version].expected_flash_sha256
            self._write(dict(schema=1, status="verified", version=self.seed_version,
                             flash_sha256=digest, baseline_sha256=rom_local.LEGACY_BASELINE_SHA256))
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("predecessor_missing")
        value = json.loads(rom_local._read_bounded(self.path, 2048), object_pairs_hook=unique)
        if (type(value) is not dict or set(value) != {"schema", "status", "version", "flash_sha256", "baseline_sha256"} or
                type(value["schema"]) is not int or value["schema"] != 1 or value["status"] != "verified" or
                not (value["version"] == "original" or version(value["version"])) or
                type(value["flash_sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", value["flash_sha256"]) is None or
                value["baseline_sha256"] != rom_local.LEGACY_BASELINE_SHA256):
            raise ValueError("predecessor_not_verified")
        if value["version"] == "original":
            expected = rom_local.LEGACY_BASELINE_SHA256
        elif value["version"] in releases:
            expected = releases[value["version"]].expected_flash_sha256
        else:
            expected = value["flash_sha256"]
        if value["flash_sha256"] != expected:
            raise ValueError("predecessor_mismatch")
        # Upgrade an already-verified predecessor from the earlier format without
        # changing its pin. Never repair/reseed missing, corrupt or pending data.
        self._mark_initialized()
        return value

    def intent(self, previous):
        self._write(dict(previous, status="pending"))

    def verified(self, version, digest):
        self._write(dict(schema=1, status="verified", version=version,
                         flash_sha256=digest, baseline_sha256=rom_local.LEGACY_BASELINE_SHA256))