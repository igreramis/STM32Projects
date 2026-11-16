"""Opt-in, single-worker OTA journal. No MQTT, hardware, or work on import.

The caller MUST authenticate inbound commands using MQTT mTLS/topic ACLs and
construct this coordinator only for explicit local OTA configuration. JSON
authorization flags are not credentials (and are rejected as unknown fields).

submit() returns a JSON-serializable immutable snapshot. Publish snapshots at
QoS 1, then call mark_delivered(command_id, event_seq) ONLY after PUBACK.
pending_results() coalesces progress to the latest durable revision per ID.
Tombstones are never evicted. A full/corrupt/unwritable journal fails closed.
close() requests cancellation and joins without a timeout: the lifetime flock
must outlive the non-daemon worker, including bounded but lengthy ROM reads.
"""

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import signed_release
from deployment_arm import DeploymentArmVerifier, ISSUED_SKEW_SECONDS


VERSIONS = ("0.1.0", "0.2.0", "0.3.0")
MAX_RECORDS = 1024
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_REQUIRED = {"command_id", "command", "version", "from_version", "sha256", "expires_at"}
_PHASES = frozenset((
    "accepted", "preparing", "downloading", "image_validated", "waiting_for_rom",
    "validating_target", "backing_up", "backup_saved", "erase_started",
    "programming", "verifying_flash", "flash_verified", "waiting_for_application",
    "application_verified", "error", "rejected",
))
_TERMINAL = frozenset(("application_verified", "error", "interrupted", "rejected"))
_ERRORS = frozenset((
    "bootloader_timeout", "bootloader_sync_failed", "target_validation_failed",
    "backup_failed", "programming_failed", "verification_failed",
    "application_start_timeout", "cancelled", "command_expired",
    "image_validation_failed", "download_not_approved", "download_failed",
    "download_validation_failed", "download_deadline_exceeded", "internal_error",
    "interrupted", "installer_busy", "storage_error", "deployment_arm_expired",
))
_FLAGS = ("flash_verified", "flash_may_be_modified", "application_health_verified")
_RECORD_KEYS = {
    "fingerprint", "command_id", "version", "status", "phase", "flashed",
    *_FLAGS, "device_id", "delivery_seq", "acked_seq",
}


class Snapshot(dict):
    """Immutable, flat dict accepted by json.dumps; never shares journal state."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("immutable OTA snapshot")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable


class StorageError(RuntimeError):
    def __init__(self):
        super().__init__("storage_error")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _safe_id(value):
    return value if type(value) is str and _ID.fullmatch(value) else None


class OtaJobs:
    """installer(command, progress, stop_event) -> dict; no worker until submit.

    Invalid/legacy-expired, conflicting, closed, unhealthy and capacity rejections are
    transient (event_seq=None); do not acknowledge them in the durable journal.
    Busy and authentic arm-expired rejections ARE durable when capacity permits.
    Duplicate requests are
    looked up before expiry so a late redelivery can retrieve its tombstone.
    """

    def __init__(self, client_id, state_root, installer, clock=time.time, *,
                 deployment_arm_verifier=None, require_deployment_arm=False):
        if (type(client_id) is not str or
                re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", client_id) is None or
                not callable(installer) or not callable(clock) or
                type(require_deployment_arm) is not bool or
                (deployment_arm_verifier is not None and not callable(deployment_arm_verifier)) or
                (require_deployment_arm and deployment_arm_verifier is None)):
            raise ValueError("invalid_ota_configuration")
        self.client_id = client_id
        self.install_enabled = True
        self.deployment_arm_verifier = deployment_arm_verifier
        self.require_deployment_arm = require_deployment_arm
        self.installer = installer
        self.clock = clock
        self.stop_event = threading.Event()  # Never the gateway's transport event.
        self._lock = threading.RLock()
        self._records = {}
        self._worker = None
        self._current_id = None
        self._closed = False
        self._healthy = True
        self._root_fd = None
        try:
            self.state_root = Path(state_root).expanduser().resolve()
            self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Lock the directory inode, not individual command files. Aliased
            # paths therefore cannot start a second worker for this state root.
            self._root_fd = os.open(str(self.state_root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(self._root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("ota_state_locked") from None
            parent_fd = os.open(str(self.state_root.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            self._load()
        except Exception as exc:
            if self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None
            if type(exc) is RuntimeError and str(exc) == "ota_state_locked":
                raise
            raise StorageError() from None

    @staticmethod
    def _filename(command_id):
        return hashlib.sha256(command_id.encode("ascii")).hexdigest() + ".json"

    @staticmethod
    def _snapshot(record):
        return Snapshot({key: value for key, value in record.items()
                         if key not in ("fingerprint", "delivery_seq", "acked_seq")},
                        event_seq=record["delivery_seq"])

    def _reject(self, command_id, error):
        return Snapshot(command_id=_safe_id(command_id), device_id=self.client_id,
                        status="error", error=error, event_seq=None)

    def _load(self):
        paths = list(self.state_root.glob("*.json"))
        if len(paths) > MAX_RECORDS:
            raise StorageError()
        for path in sorted(paths):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
                raise StorageError()
            record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            if (type(record) is not dict or
                    set(record) not in (_RECORD_KEYS, _RECORD_KEYS | {"error"}) or
                    _safe_id(record.get("command_id")) is None or
                    path.name != self._filename(record["command_id"]) or
                    record.get("device_id") != self.client_id or
                    type(record.get("fingerprint")) is not str or
                    re.fullmatch(r"[0-9a-f]{64}", record["fingerprint"]) is None or
                    not signed_release.version(record.get("version")) or
                    record.get("phase") not in _PHASES or
                    record.get("status") not in _PHASES | _TERMINAL | {"running"} or
                    any(type(record.get(key)) is not bool for key in _FLAGS) or
                    (record.get("flashed") is not None and type(record.get("flashed")) is not bool) or
                    type(record.get("delivery_seq")) is not int or record["delivery_seq"] < 1 or
                    type(record.get("acked_seq")) is not int or
                    not 0 <= record["acked_seq"] <= record["delivery_seq"] or
                    ("error" in record and record["error"] not in _ERRORS)):
                raise StorageError()
            expected = True if record["flash_verified"] else (
                None if record["flash_may_be_modified"] else False)
            if (record["flashed"] is not expected or
                    (record["flash_verified"] and not record["flash_may_be_modified"]) or
                    (record["application_health_verified"] and not record["flash_verified"]) or
                    (record["status"] == "application_verified" and
                     not record["application_health_verified"])):
                raise StorageError()
            self._records[record["command_id"]] = record
            self._current_id = record["command_id"]
        # Never re-run unfinished work. Keep the last phase and sticky evidence.
        for record in list(self._records.values()):
            if record["status"] not in _TERMINAL:
                updated = dict(record, status="interrupted", error="interrupted",
                               delivery_seq=record["delivery_seq"] + 1)
                self._commit(updated)

    def _atomic_write(self, record):
        """Commit point: file fsync -> atomic replace -> directory fsync."""
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".ota-", dir=str(self.state_root))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(self.state_root / self._filename(record["command_id"])))
            os.fsync(self._root_fd)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _commit(self, record):
        if not self._healthy:
            raise StorageError()
        try:
            self._atomic_write(record)
        except Exception:
            # replace may already have happened. Keep attempted safety evidence
            # in memory, but NEVER claim this revision is durable or publish it.
            self._records[record["command_id"]] = dict(record)
            self._healthy = False
            self.stop_event.set()
            raise StorageError() from None
        self._records[record["command_id"]] = record

    @staticmethod
    def _command(value):
        if type(value) is not dict:
            raise ValueError
        command = dict(value)
        dynamic = "release" in command
        if (set(command) not in (_REQUIRED, _REQUIRED | {"url"}, _REQUIRED | {"url", "release"},
                    _REQUIRED | {"url", "release", "deployment_arm"}) or
                _safe_id(command.get("command_id")) is None or
                command.get("command") != "install_firmware" or
                    not (signed_release.version(command.get("version")) if dynamic else command.get("version") in VERSIONS) or
                    not (command.get("from_version") == "original" or
                       (signed_release.version(command.get("from_version")) if dynamic else
                        command.get("from_version") in VERSIONS)) or
                type(command.get("sha256")) is not str or
                _HASH.fullmatch(command["sha256"]) is None or
                type(command.get("expires_at")) is not int or
                ("url" in command and (type(command["url"]) is not str or
                                       len(command["url"]) > 3500))):
            raise ValueError
        serialized = json.dumps(
            command, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False).encode("ascii")
        if len(serialized) > 8192:
            raise ValueError
        fingerprint = hashlib.sha256(serialized).hexdigest()
        return json.loads(serialized), fingerprint

    def submit(self, value):
        command_id = value.get("command_id") if type(value) is dict else None
        try:
            command, fingerprint = self._command(value)
        except (ValueError, TypeError, OverflowError, RecursionError):
            return self._reject(command_id, "invalid_arguments")
        command_id = command["command_id"]
        with self._lock:
            if not self._healthy:
                return self._reject(command_id, "storage_error")
            existing = self._records.get(command_id)
            if existing is not None:
                return (self._snapshot(existing) if existing["fingerprint"] == fingerprint
                        else self._reject(command_id, "command_id_reused"))
            if self._closed or self.stop_event.is_set():
                return self._reject(command_id, "coordinator_closed")
            if not self.install_enabled:
                return self._reject(command_id, "deployment_disabled")
            permit = None
            if "deployment_arm" in command or self.require_deployment_arm:
                if "deployment_arm" not in command or self.deployment_arm_verifier is None:
                    return self._reject(command_id, "invalid_deployment_arm")
                try:
                    permit = self.deployment_arm_verifier(command)
                except Exception:
                    return self._reject(command_id, "invalid_deployment_arm")
            # Read time AFTER crypto verification; never admit against a stale clock sample.
            now = self.clock()
            rejection = None
            if permit is not None:
                if not math.isfinite(now) or permit["issued_at"] > now + ISSUED_SKEW_SECONDS:
                    return self._reject(command_id, "invalid_deployment_arm")
                if now >= permit["arm_expires_at"]:
                    rejection = "deployment_arm_expired"
            if rejection is None and (not math.isfinite(now) or not now < command["expires_at"] <= now + 1800):
                return self._reject(command_id, "command_expired")
            if len(self._records) >= MAX_RECORDS:
                return self._reject(command_id, "state_capacity_exceeded")
            busy = self._worker is not None and self._worker.is_alive()
            if rejection is None and busy:
                rejection = "installer_busy"
            record = dict(fingerprint=fingerprint, command_id=command_id,
                          version=command["version"], device_id=self.client_id,
                          status="rejected" if rejection else "accepted",
                          phase="rejected" if rejection else "accepted", flashed=False,
                          flash_verified=False, flash_may_be_modified=False,
                          application_health_verified=False, delivery_seq=1, acked_seq=0)
            if rejection:
                record["error"] = rejection
            try:
                self._commit(record)  # Must finish before Thread.start().
            except StorageError:
                return self._reject(command_id, "storage_error")
            response = self._snapshot(record)
            if rejection is None:
                self._current_id = command_id
                # Hash the original request, but match Installer's lowercase SHA API.
                command["sha256"] = command["sha256"].lower()
                try:
                    self._worker = threading.Thread(target=self._run, args=(command,),
                                                    name="ota-installer", daemon=False)
                    self._worker.start()
                except Exception:
                    self._finish(command_id, {"status": "error", "error": "internal_error"})
                    if not self._healthy:
                        return self._reject(command_id, "storage_error")
                    return self._snapshot(self._records[command_id])
            return response

    @staticmethod
    def _merge_flags(record, details):
        for key in _FLAGS:
            if details.get(key) is True:
                record[key] = True
        if record["application_health_verified"]:
            record["flash_verified"] = True
        if record["flash_verified"]:
            record["flash_may_be_modified"] = True
        record["flashed"] = (True if record["flash_verified"] else
                             None if record["flash_may_be_modified"] else False)

    def _progress(self, command_id, stage, **details):
        with self._lock:
            if not self._healthy:
                raise StorageError()
            if self.stop_event.is_set():
                raise RuntimeError("cancelled")
            if type(stage) is not str or stage not in _PHASES - {"accepted", "rejected", "error"}:
                raise ValueError("invalid_installer_progress")
            old = self._records[command_id]
            record = dict(old, phase=stage, status="running" if stage == "application_verified" else stage,
                          delivery_seq=old["delivery_seq"] + 1)
            # Intent alone means uncertain flash, even when flags are omitted.
            if stage in {"erase_started", "programming", "verifying_flash", "flash_verified",
                         "waiting_for_application", "application_verified"}:
                record["flash_may_be_modified"] = True
            self._merge_flags(record, details)
            self._commit(record)

    def _finish(self, command_id, result):
        with self._lock:
            if not self._healthy:
                return
            old = self._records[command_id]
            record = dict(old, delivery_seq=old["delivery_seq"] + 1)
            if type(result) is not dict:
                result = {"status": "error", "error": "internal_error"}
            self._merge_flags(record, result)
            if result.get("status") == "application_verified" and record["application_health_verified"]:
                record.update(status="application_verified", phase="application_verified")
                record.pop("error", None)
            else:
                error = result.get("error")
                record.update(status="error", error=error if type(error) is str and error in _ERRORS
                              else "internal_error")
                # Preserve last phase to explain an error/interruption safely.
            try:
                self._commit(record)
            except StorageError:
                pass  # Fail closed; never retry a possibly committed replacement.

    def _run(self, command):
        command_id = command["command_id"]
        try:
            if self.stop_event.is_set():
                result = {"status": "error", "error": "cancelled"}
            elif self.clock() >= command["expires_at"]:
                result = {"status": "error", "error": "command_expired"}
            else:
                result = self.installer(command, lambda stage, **details:
                                        self._progress(command_id, stage, **details), self.stop_event)
        except BaseException:
            # Never log exception text/traceback: it may include signed URLs.
            result = {"status": "error", "error": "internal_error"}
        finally:
            command.clear()  # The coordinator retains no URL after this call.
        self._finish(command_id, result)

    def pending_results(self):
        with self._lock:
            if not self._healthy:
                raise StorageError()
            return [self._snapshot(record) for record in self._records.values()
                    if record["delivery_seq"] > record["acked_seq"]]

    def mark_delivered(self, command_id, event_seq):
        """Return True only for this exact durable revision's PUBACK."""
        with self._lock:
            if not self._healthy:
                raise StorageError()
            if self._closed or _safe_id(command_id) is None or type(event_seq) is not int:
                return False
            record = self._records.get(command_id)
            if record is None or event_seq != record["delivery_seq"]:
                return False
            if record["acked_seq"] != event_seq:
                self._commit(dict(record, acked_seq=event_seq))
            return True

    def status(self):
        with self._lock:
            record = self._records.get(self._current_id)
            result = dict(self._snapshot(record)) if record else {"status": "idle"}
            if not self._healthy:
                result.update(status="error", error="storage_error", event_seq=None)
            result.update(active=self._worker is not None and self._worker.is_alive(),
                          healthy=self._healthy, closed=self._closed)
            return Snapshot(result)

    def close(self):
        with self._lock:
            self._closed = True
            self.stop_event.set()
            worker = self._worker
        if worker is threading.current_thread():
            raise RuntimeError("cannot_close_from_ota_worker")
        if worker is not None and worker.ident is not None:
            worker.join()  # No lock held: progress/final persistence needs it.
        with self._lock:
            if self._root_fd is not None:
                monitor = getattr(self, "monitor", None)
                if monitor is not None:
                    # The process lock must also cover the receive-only owner.
                    # Failed close retains the lock until process exit/recovery.
                    monitor.close()
                os.close(self._root_fd)
                self._root_fd = None

    @classmethod
    def from_config(cls, path, client_id):
        """None disables OTA without imports/I/O; otherwise strict local config.

        Resource contents and serial availability are checked by Installer only
        in its worker. Relative paths (including download metadata) resolve
        against the config directory, never the process working directory.

        Optional ``signed_releases`` has exactly public_key (local PEM path),
        key_id (exact KMS ARN), bucket, region, seed_version. Seed must be an
        existing catalog version or 'original', explicitly known by the operator.
        The full backup must still match that pin before erase. Predecessor
        state lives in state_root/verified-flash; never delete it to recover an
        interrupted job. Operator investigation is required for pending state.
        Optional ``uart_monitor: true`` enables the receive-only monitor inside
        this same client/service process; default is disabled. Stop external
        UART monitors before enabling. No service or live config is rewritten.
        Optional ``require_deployment_arm`` is a strict boolean, default false;
        true requires signed_releases. Supplied permits are always verified.
        install_enabled remains the independent emergency admission master.
        Signed operation requires the optional cryptography dependency locally.
        """
        if path is None:
            return None
        try:
            config_path = Path(path).expanduser().resolve()
            config = json.loads(config_path.read_text(encoding="utf-8"),
                                object_pairs_hook=_unique_object)
            required = {"catalog", "release_root", "baseline", "run_root", "state_root", "port"}
            optional = {"download_releases", "signed_releases", "uart_monitor", "install_enabled",
                        "require_deployment_arm"}
            if type(config) is not dict or not required <= set(config) or set(config) - required - optional:
                raise ValueError

            def resolve(value):
                if type(value) is not str or not value or "\x00" in value:
                    raise ValueError
                candidate = Path(value).expanduser()
                return str((config_path.parent / candidate).resolve())

            kwargs = {key: resolve(config[key]) for key in required}
            downloads = config.get("download_releases", {})
            if type(downloads) is not dict or any(key not in VERSIONS for key in downloads):
                raise ValueError
            kwargs["download_releases"] = {key: resolve(value) for key, value in downloads.items()}
            signing = config.get("signed_releases")
            require_arm = config.get("require_deployment_arm", False)
            if type(require_arm) is not bool or (require_arm and signing is None):
                raise ValueError
            arm_verifier = None
            if "signed_releases" in config:
                if (type(signing) is not dict or set(signing) != {"public_key", "key_id", "bucket", "region", "seed_version"} or
                        signing["seed_version"] not in ("original",) + VERSIONS):
                    raise ValueError
                kwargs["verifier"] = signed_release.ReleaseVerifier(resolve(signing["public_key"]),
                    signing["key_id"], signing["bucket"], signing["region"])
                arm_verifier = DeploymentArmVerifier(kwargs["verifier"], client_id)
                from verified_state import VerifiedState
                kwargs["predecessor_state"] = VerifiedState(Path(kwargs["state_root"]) / "verified-flash", signing["seed_version"])
            monitor_enabled = config.get("uart_monitor", False)
            install_enabled = config.get("install_enabled", True)
            if type(monitor_enabled) is not bool or type(install_enabled) is not bool:
                raise ValueError
        except Exception:
            raise ValueError("invalid_ota_config") from None
        from ota_installer import Installer  # Explicit opt-in only; no hardware opened here.
        state_root = kwargs.pop("state_root")
        installer = Installer(**kwargs)
        monitor = None
        if monitor_enabled:
            from uart_state import StateMonitor, MonitoredInstaller
            monitor = StateMonitor(kwargs["port"])
            installer = MonitoredInstaller(installer, monitor)
        jobs = cls(client_id, state_root, installer, deployment_arm_verifier=arm_verifier,
               require_deployment_arm=require_arm)
        jobs.install_enabled = install_enabled
        jobs.monitor = monitor
        return jobs


def from_config(path, client_id):
    return OtaJobs.from_config(path, client_id)