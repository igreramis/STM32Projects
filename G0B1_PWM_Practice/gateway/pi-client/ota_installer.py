"""Synchronous, locally approved OTA worker; never resets or boots the target.

The coordinator owns authorization, a single worker and the process-wide flock.
Call only in that worker: rom_local._configured_engine temporarily changes engine
globals. No MQTT, replay cache, deployment or GPIO policy belongs here.

Installer(...)(command, progress, stop_event) returns a JSON-safe result, including
on failure. Helpers raise InstallerError with a fixed, non-secret ``code``. A
progress callback must durably commit before returning; failure stops the worker.
``flashed`` is False before destructive I/O, None on uncertain flash, and True
only after full readback verification (even if subsequent app startup fails).
"""

from contextlib import contextmanager
from pathlib import Path
import re
import time

import firmware
import heartbeat_monitor
import rom_local
import rom_program as engine
import signed_release
from rom_read_id import ACK, NACK, RomProbe


ROM_WAIT_SECONDS = 120.0
ROM_MAX_ATTEMPTS = 60
ROM_PROBE_INTERVAL = 2.0
ROM_NOISE_LIMIT = 8192
APPLICATION_WAIT_SECONDS = 120.0
ERROR_CODES = frozenset((
    "bootloader_timeout", "bootloader_sync_failed", "target_validation_failed",
    "backup_failed", "programming_failed", "verification_failed",
    "application_start_timeout", "cancelled", "command_expired",
    "image_validation_failed", "download_not_approved", "download_failed",
    "download_validation_failed", "download_deadline_exceeded",
))


class InstallerError(Exception):
    """Only fixed error codes can leave this module's helpers."""

    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "programming_failed"
        super().__init__(self.code)


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise InstallerError("cancelled")


def wait_for_rom(port, clock=time.monotonic, stop_event=None, progress=None):
    """Sync only; one initial clear, <=60 probes, <=120s including writes/reads.

    Read slices keep cancellation responsive. Probe start-to-start spacing
    includes response time, not an extra two-second sleep. Before another probe,
    inspect queued bytes without clearing them, so a late ACK ends sync too.
    A NACK/short write is ambiguous protocol state: stop, require manual reset.
    The fake port used with a fake clock must advance that clock on timed reads.
    """
    try:
        _check_stop(stop_event)
        port.reset_input_buffer()
        if progress is not None:
            progress("waiting_for_rom")
        print("Waiting for ROM mode: set BOOT0 HIGH and press/release B2.", flush=True)
        deadline = clock() + ROM_WAIT_SECONDS
        noise = 0

        def receive(timeout):
            nonlocal noise
            _check_stop(stop_event)
            remaining = deadline - clock()
            if remaining <= 0:
                raise InstallerError("bootloader_timeout")
            port.timeout = max(0.0, min(timeout, remaining))
            value = port.read(1)
            _check_stop(stop_event)
            if clock() >= deadline:
                raise InstallerError("bootloader_timeout")
            if value == bytes((NACK,)):
                raise InstallerError("bootloader_sync_failed")
            if value == bytes((ACK,)):
                return True, True
            noise += len(value)
            if noise > ROM_NOISE_LIMIT:
                raise InstallerError("bootloader_sync_failed")
            return False, bool(value)

        for attempt in range(ROM_MAX_ATTEMPTS):
            _check_stop(stop_event)
            if clock() >= deadline:
                break
            if attempt:
                # Never reset_input_buffer after a probe. Consume late ACKs
                # before sending another 7F into an already synchronized ROM.
                while True:
                    ack, had_data = receive(0.0)
                    if ack:
                        return
                    if not had_data:
                        break
            started = clock()
            if started >= deadline:
                break
            attempt_deadline = min(deadline, started + ROM_PROBE_INTERVAL)
            port.write_timeout = min(ROM_PROBE_INTERVAL, deadline - started)
            if port.write(b"\x7f") != 1:
                raise InstallerError("bootloader_sync_failed")
            while clock() < attempt_deadline:
                ack, _ = receive(min(0.25, attempt_deadline - clock()))
                if ack:
                    return
        raise InstallerError("bootloader_timeout")
    except InstallerError:
        raise
    except Exception:
        raise InstallerError("bootloader_sync_failed") from None


class WaitingRomProbe(RomProbe):
    """Change sync only; inherited identification and command behavior stay fixed."""

    def __init__(self, port, clock=time.monotonic, stop_event=None, progress=None):
        super().__init__(port, timeout=2.0, clock=clock)
        self.stop_event = stop_event
        self.progress = progress
        self._sync_called = False

    def sync(self):
        if self._sync_called:
            raise InstallerError("bootloader_sync_failed")
        self._sync_called = True
        wait_for_rom(self.port, self.clock, self.stop_event, self.progress)


def _wait_for_application(port, version, clock, stop_event):
    """Two fresh exact lines, bounded like heartbeat_monitor, without echoing RX.

    Arbitrary UART noise may contain secrets, so only match counts are logged.
    The caller clears input before announcing the manual BOOT0 LOW/B2 step.
    """
    deadline = clock() + APPLICATION_WAIT_SECONDS
    pending = bytearray()
    matches = 0
    while matches < 2:
        _check_stop(stop_event)
        remaining = deadline - clock()
        if remaining <= 0:
            raise InstallerError("application_start_timeout")
        port.timeout = min(0.25, remaining)
        chunk = port.read(1)
        _check_stop(stop_event)
        if clock() >= deadline:
            raise InstallerError("application_start_timeout")
        if chunk == b"\n":
            line = bytes(pending)
            if line.endswith(b"\r"):
                line = line[:-1]
            if heartbeat_monitor.heartbeat_version(line, allow_dynamic=True) == version:
                matches += 1
                print("application heartbeat: {}/2 exact matches".format(matches), flush=True)
            pending.clear()
        else:
            pending.extend(chunk)
            if len(pending) > heartbeat_monitor.MAX_LINE_BYTES:
                raise InstallerError("application_start_timeout")


_PHASE_ERRORS = {
    "new": "bootloader_sync_failed", "identifying": "target_validation_failed",
    "backup": "backup_failed", "backed-up": "backup_failed",
    "erase-authorized": "programming_failed",
    "erase-acknowledged": "programming_failed",
    "erased-verified": "programming_failed", "written": "verification_failed",
    "verified": "verification_failed",
}


class _ProgressRecord(engine.RunRecord):
    def __init__(self, root, notify, authorize, result):
        self.notify = notify
        self.authorize = authorize
        self.result = result
        self.failed_phase = None
        super().__init__(root)

    def event(self, name, **details):
        self.result["run_directory"] = str(self.path)
        if name == "failed":
            self.failed_phase = details["phase"]
        if name == "verified":
            # Engine emits this only AFTER full readback and preserved-tail
            # comparison. Retain the evidence even if journaling then fails.
            self.result.update(flashed=True, flash_verified=True,
                               flash_may_be_modified=True,
                               flash_sha256=details["flash_sha256"])
        if name == "erase-intent":
            self.authorize()
            # Both journals must succeed before Programmer sets its destructive
            # flag or sends 44 BB. A failed callback MUST propagate, not retry.
            super().event(name, **details)
            self.notify("erase_started", flash_may_be_modified=True)
            # The callback/fsync can take time or trigger cancellation.
            self.authorize()
            return
        super().event(name, **details)
        stage = {
            "created": "preparing", "rom-identity": "validating_target",
            "identified": "backing_up", "erased-verified": "programming",
            "write-acknowledged": "verifying_flash",
        }.get(name)
        if name == "snapshot" and details.get("file") == "flash-before.bin":
            stage = "backup_saved"
        if stage is not None:
            self.notify(stage)
        # Announce flash_verified only AFTER ROM close and app input clear.


class Installer:
    def __init__(self, catalog, release_root, baseline, run_root,
                 port='/dev/serial0', download_releases=None, verifier=None, predecessor_state=None):
        self.catalog = Path(catalog)
        self.release_root = Path(release_root)
        self.baseline = Path(baseline)
        self.run_root = Path(run_root)
        self.port = port
        self.download_releases = dict(download_releases or {})
        self.verifier = verifier
        self.predecessor_state = predecessor_state
        # Injectable clocks for software tests; not remote command fields.
        self.clock = time.monotonic
        self.wall_clock = time.time
        self.uart_released = True

    @contextmanager
    def _port(self, factory):
        self.uart_released = False
        port = factory()
        try:
            yield port
        finally:
            port.close()
            self.uart_released = True

    @staticmethod
    def _command(command):
        required = {"command_id", "command", "version", "from_version", "sha256", "expires_at"}
        dynamic = type(command) is dict and "release" in command
        if (type(command) is not dict or set(command) not in (required, required | {"url"}, required | {"url", "release"}) or
                command.get("command") != "install_firmware" or
                not isinstance(command.get("command_id"), str) or
                re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", command["command_id"]) is None or
                 not (signed_release.version(command.get("version")) if dynamic else
                     command.get("version") in rom_local.VERSIONS) or
                 not (command.get("from_version") == "original" or
                     (signed_release.version(command.get("from_version")) if dynamic else
                      command.get("from_version") in rom_local.VERSIONS)) or
                type(command.get("expires_at")) is not int or
                not isinstance(command.get("sha256"), str) or
                re.fullmatch(r"[0-9a-f]{64}", command["sha256"]) is None):
            raise InstallerError("image_validation_failed")
        # Snapshot the parent's validated command, not its mutable dictionary.
        return dict(command)

    def _download_metadata(self, command, target):
        if "url" not in command:
            return None
        try:
            path = Path(self.download_releases[command["version"]])
            if not path.is_absolute():
                raise ValueError
            release = firmware.Release.load(path)
            if (release.version, release.size_bytes, release.sha256) != (
                    command["version"], target.size_bytes, target.sha256):
                raise ValueError
            return release
        except Exception:
            raise InstallerError("download_not_approved") from None

    @staticmethod
    def _download(release, command, directory):
        try:
            firmware.FirmwareStager(release, directory).stage({
                "command_id": command["command_id"], "command": "stage_firmware",
                "version": command["version"], "url": command["url"],
            })
        except firmware.StageError as exc:
            code = str(exc)
            if code == "download_deadline_exceeded":
                raise InstallerError(code) from None
            if code in {"invalid_stage_fields", "release_not_approved", "invalid_download_url",
                        "url_not_approved", "redirect_refused", "s3_version_mismatch",
                        "size_mismatch", "sha256_mismatch", "content_encoding_refused"}:
                raise InstallerError("download_validation_failed") from None
            raise InstallerError("download_failed") from None
        except Exception:
            raise InstallerError("download_failed") from None
        # Never trust a downloader-returned path, and never use the old staging slot.
        return directory / "firmware.bin"

    def __call__(self, command, progress, stop_event):
        result = dict(status="error", flashed=False, flash_verified=False,
                      flash_may_be_modified=False, application_health_verified=False,
                      version=None, image_sha256=None, flash_sha256=None,
                      run_directory=None)
        programmer = record = None
        previous = None
        error_code = "image_validation_failed"

        def notify(stage, **details):
            if programmer is not None and programmer.destructive_started:
                result["flash_may_be_modified"] = True
                if not result["flash_verified"]:
                    result["flashed"] = None
            try:
                flags = {key: value for key, value in result.items() if key != "status"}
                progress(stage, **dict(flags, **details))
                if stage == "erase_started" and self.predecessor_state is not None:
                    self.predecessor_state.intent(previous)
            except Exception:
                code = (_PHASE_ERRORS.get(programmer.phase, error_code)
                        if programmer is not None and programmer.phase not in ("verified", "failed")
                        else error_code)
                raise InstallerError(code) from None
            print("ota: command_id={} version={} phase={}".format(
                command["command_id"], command["version"], stage), flush=True)

        try:
            command = self._command(command)
            result["version"] = command["version"]
            if not self.uart_released:
                raise InstallerError("bootloader_sync_failed")

            def authorized():
                _check_stop(stop_event)
                if self.wall_clock() >= command["expires_at"]:
                    raise InstallerError("command_expired")

            authorized()
            if command["expires_at"] > self.wall_clock() + 1800:
                raise InstallerError("command_expired")
            dynamic = "release" in command
            download = None
            if dynamic:
                if self.verifier is None or self.predecessor_state is None:
                    raise InstallerError("download_not_approved")
                download = self.verifier.command(command)
            releases = rom_local.validate_catalog(self.catalog, self.release_root, self.baseline)
            if self.predecessor_state is not None:
                previous = self.predecessor_state.load(releases)
                if previous["version"] != command["from_version"]:
                    raise InstallerError("backup_failed")
                predecessor = previous["flash_sha256"]
            else:
                predecessor = (rom_local.LEGACY_BASELINE_SHA256 if command["from_version"] == "original"
                               else releases[command["from_version"]].expected_flash_sha256)
            if dynamic:
                metadata = rom_local.LocalRelease(Path("."), download.size_bytes, download.sha256, "")
                with rom_local._configured_engine(metadata, predecessor):
                    record = _ProgressRecord(self.run_root, notify, authorized, result)
                notify("downloading")
                selected = self._download(download, command, record.path / "download")
                data = rom_local._read_bounded(selected, 20480)
                signed_release.validate_image(data, download)
                baseline = rom_local._read_bounded(self.baseline, rom_local.FLASH_SIZE)
                if len(baseline) != rom_local.FLASH_SIZE or engine.sha256(baseline) != rom_local.LEGACY_BASELINE_SHA256:
                    raise InstallerError("image_validation_failed")
                target = rom_local.LocalRelease(selected, len(data), download.sha256,
                    engine.sha256(data + b"\xff" * (rom_local.ERASE_SIZE - len(data)) + baseline[rom_local.ERASE_SIZE:]))
                if command["version"] in releases and target.sha256 != releases[command["version"]].sha256:
                    raise InstallerError("image_validation_failed")
            else:
                target = releases[command["version"]]
                download = self._download_metadata(command, target)
            if command["sha256"] != target.sha256:
                raise InstallerError("image_validation_failed")
            result["image_sha256"] = target.sha256

            with rom_local._configured_engine(target, predecessor):
                error_code = "backup_failed"
                if record is None:
                    record = _ProgressRecord(self.run_root, notify, authorized, result)
                selected = target.path
                if download is not None and not dynamic:
                    error_code = "download_failed"
                    authorized()
                    notify("downloading")
                    selected = self._download(download, command, record.path / "download")
                error_code = "image_validation_failed"
                image = engine.load_image(selected)  # immutable, revalidated BEFORE serial
                notify("image_validated")
                authorized()
                error_code = "bootloader_sync_failed"
                import serial
                # Check again after import and immediately before open.
                authorized()
                if Path(self.port).resolve() != Path("/dev/ttyAMA0"):
                    raise InstallerError("bootloader_sync_failed")
                with self._port(lambda: engine.open_port(serial, self.port, 115200, 2.0)) as port:
                    programmer = engine.Programmer(port, image, record, clock=self.clock)
                    programmer.io = WaitingRomProbe(port, self.clock, stop_event, notify)

                    def confirm(_prompt):
                        if programmer.phase != "backed-up":
                            raise InstallerError("backup_failed")
                        authorized()
                        return engine.CONFIRMATION

                    error_code = "target_validation_failed"
                    programmed = programmer.run(confirm)
                    error_code = "verification_failed"
                    if (programmed["status"] != "flash_verified" or
                            programmed["flash_sha256"] != target.expected_flash_sha256):
                        raise InstallerError("verification_failed")
                    if self.predecessor_state is not None:
                        self.predecessor_state.verified(command["version"], programmed["flash_sha256"])

            # No app port or boot guidance on any ROM/programming failure.
            error_code = "application_start_timeout"
            _check_stop(stop_event)
            with self._port(lambda: heartbeat_monitor.open_port(serial, self.port)) as port:
                port.reset_input_buffer()  # Must precede the operator's boot instruction.
                notify("flash_verified")
                print("Flash verified. Set BOOT0 LOW and press/release B2.", flush=True)
                notify("waiting_for_application")
                _wait_for_application(port, command["version"], self.clock, stop_event)
                result.update(status="application_verified", application_health_verified=True)
                notify("application_verified")
            return result
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, InstallerError):
                code = exc.code
            elif isinstance(exc, KeyboardInterrupt):
                code = "cancelled"
            elif programmer is not None and programmer.phase == "failed":
                code = _PHASE_ERRORS.get(record.failed_phase, error_code)
            else:
                code = error_code
            if programmer is not None and programmer.destructive_started:
                result["flash_may_be_modified"] = True
                if not result["flash_verified"]:
                    result["flashed"] = None
            result.update(status="error", error=code)
            print("ota error: " + code + "; stopped, no automatic boot or retry.", flush=True)
            if code == "bootloader_sync_failed":
                print("ROM sync failed: manual reset required before another job.", flush=True)
            if result["flash_may_be_modified"] and not result["flash_verified"]:
                print("FLASH MAY BE INCOMPLETE. Keep BOOT0 HIGH; do not boot the application.", flush=True)
            return result