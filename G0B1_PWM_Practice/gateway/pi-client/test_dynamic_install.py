"""Real installer and independent ROM emulator; downloads are in-memory only."""
import json
from pathlib import Path
import threading
from unittest.mock import Mock, patch

import client
import ota_installer
import ota_jobs
import rom_program
import uart_state
import verified_state
from test_ota_installer import InstallerFixture, WorkerRom, line
from test_signed_release import key_fixture, payload, envelope, url


class Response:
    status = 200

    def __init__(self, data, metadata):
        self.data = data
        self.headers = {"x-amz-version-id": metadata["version_id"], "Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read1(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result


class DynamicTests(InstallerFixture):
    def setUp(self):
        super().setUp()
        self.key, verifier = key_fixture(self.root)
        self.installer.verifier = verifier
        self.state = verified_state.VerifiedState(self.root / "verified", "0.1.0")
        self.installer.predecessor_state = self.state
        self.dynamic("7.8.9", "0.1.0")

    def dynamic(self, version, predecessor):
        self.image = ((0x20024000).to_bytes(4, "little") + (0x08000009).to_bytes(4, "little") +
                      b"Hello from NUCLEO-G0B1RE! firmware=" + version.encode() + b"\r\n\x00")
        self.metadata = payload(version, self.image)
        self.command = dict(command_id="job-" + version, command="install_firmware", version=version,
                            from_version=predecessor, sha256=self.metadata["sha256"], expires_at=2000,
                            url=url(self.metadata), release=envelope(self.key, self.metadata))
        self.future_app = line(version) * 2
        self.opener.open.side_effect = lambda *a, **k: Response(self.image, self.metadata)

    def test_two_arbitrary_new_versions_with_restart_and_full_flash_proof(self):
        catalog_before = self.catalog_path.read_bytes()
        first = self.run_installer()
        self.assertEqual(first["status"], "application_verified", first)
        expected = self.full_flash(self.image)
        self.assertEqual(bytes(self.rom.memory), expected)
        self.assertEqual(self.rom.erased_pages, list(range(10)))
        self.assertEqual((Path(first["run_directory"]) / "flash-before.bin").stat().st_size, 524288)
        self.assertEqual((Path(first["run_directory"]) / "flash-after.bin").read_bytes(), expected)
        # New process object loads the same persisted predecessor, not the catalog.
        self.installer.predecessor_state = verified_state.VerifiedState(self.state.root, "original")
        self.rom = WorkerRom(self.clock, self.timeline)
        self.rom.memory = bytearray(expected)
        self.dynamic("12.34.56", "7.8.9")
        second = self.run_installer()
        self.assertEqual(second["status"], "application_verified", second)
        self.assertEqual(bytes(self.rom.memory), self.full_flash(self.image))
        persisted = json.loads(self.state.path.read_text())
        self.assertEqual(persisted["version"], "12.34.56")
        self.assertEqual(persisted["flash_sha256"], rom_program.sha256(self.full_flash(self.image)))
        self.assertEqual(self.catalog_path.read_bytes(), catalog_before)

    def test_invalid_signature_before_download_or_serial(self):
        self.command["release"]["signature"] = "a" * 344
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.opener.open.assert_not_called()
        self.assert_no_open()

    def test_signed_wrong_object_response_version_no_serial(self):
        response = Response(self.image, dict(self.metadata, version_id="wrong"))
        self.opener.open.side_effect = lambda *a, **k: response
        self.assert_error(self.run_installer(), "download_validation_failed")
        self.assert_no_open()

    def test_signed_old_version_cannot_replace_catalog_binary(self):
        self.dynamic("0.2.0", "0.1.0")
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_oversized_signed_image_rejected_before_download(self):
        self.metadata["size_bytes"] = 20481
        self.command["release"] = envelope(self.key, self.metadata)
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.opener.open.assert_not_called()
        self.assert_no_open()

    def test_missing_explicit_signer_or_persistence_never_downloads(self):
        for field in ("verifier", "predecessor_state"):
            original = getattr(self.installer, field)
            setattr(self.installer, field, None)
            self.assert_error(self.run_installer(), "download_not_approved")
            self.opener.open.assert_not_called()
            self.assert_no_open()
            setattr(self.installer, field, original)

    def test_signed_image_wrong_embedded_version_no_serial(self):
        self.image = self.image.replace(b"7.8.9", b"7.8.8")
        self.metadata = payload("7.8.9", self.image)
        self.command.update(sha256=self.metadata["sha256"], release=envelope(self.key, self.metadata))
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_arbitrary_backup_never_becomes_seed(self):
        self.rom.memory[-1] ^= 1
        self.assert_error(self.run_installer(), "backup_failed")
        self.assertNotIn(0x44, self.rom.commands)
        self.assertEqual(json.loads(self.state.path.read_text())["version"], "0.1.0")

    def test_wrong_claimed_predecessor_no_open(self):
        self.command["from_version"] = "9.9.9"
        self.assert_error(self.run_installer(), "backup_failed")
        self.assert_no_open()

    def test_pending_after_flash_commit_failure_blocks_restart_before_open(self):
        with patch.object(self.state, "verified", side_effect=OSError("simulated crash")):
            result = self.run_installer()
        self.assert_error(result, "verification_failed", flashed=True)
        self.assertEqual(json.loads(self.state.path.read_text())["status"], "pending")
        self.serial.Serial.reset_mock()
        self.installer.predecessor_state = verified_state.VerifiedState(self.state.root, "0.1.0")
        result = self.run_installer()
        self.assert_error(result, "image_validation_failed")
        self.serial.Serial.assert_not_called()

    def test_crash_after_erase_intent_never_retries(self):
        original = rom_program.Programmer._erase

        def crash(programmer):
            original(programmer)
            raise KeyboardInterrupt()

        with patch.object(rom_program.Programmer, "_erase", crash):
            self.assert_error(self.run_installer(), "cancelled", flashed=None)
        self.assertEqual(json.loads(self.state.path.read_text())["status"], "pending")
        self.serial.Serial.reset_mock()
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.serial.Serial.assert_not_called()

    def test_rom_close_failure_blocks_application_and_later_open(self):
        self.rom.close = Mock(side_effect=OSError("uncertain close"))
        result = self.run_installer()
        self.assert_error(result, "verification_failed", flashed=True)
        self.assertFalse(self.app.opened)
        self.assertFalse(self.installer.uart_released)
        self.serial.Serial.reset_mock()
        self.assert_error(self.run_installer(), "bootloader_sync_failed")
        self.serial.Serial.assert_not_called()

    def test_state_replace_failure_before_erase_preserves_previous(self):
        self.state.load(self.validate())
        original = verified_state.os.replace

        def replace(source, destination):
            if Path(destination) == self.state.path:
                raise OSError("replace failure")
            return original(source, destination)

        with patch.object(verified_state.os, "replace", side_effect=replace):
            self.assert_error(self.run_installer(), "programming_failed")
        self.assertNotIn(0x44, self.rom.commands)
        self.assertEqual(json.loads(self.state.path.read_text())["status"], "verified")
        with self.assertRaises(ValueError):
            self.state.load(self.validate())

    def test_failed_intent_never_erases(self):
        with patch.object(self.state, "intent", side_effect=OSError("storage failure")):
            self.assert_error(self.run_installer(), "programming_failed")
        self.assertNotIn(0x44, self.rom.commands)

    def test_bad_full_readback_leaves_pending_state(self):
        self.rom.change_outside = True
        result = self.run_installer()
        self.assert_error(result, "verification_failed", flashed=None)
        self.assertEqual(json.loads(self.state.path.read_text())["status"], "pending")

    def test_application_timeout_retains_verified_predecessor(self):
        self.future_app = b""
        result = self.run_installer()
        self.assert_error(result, "application_start_timeout", flashed=True)
        self.assertEqual(json.loads(self.state.path.read_text())["version"], "7.8.9")

    def test_missing_or_corrupt_existing_state_never_reseeds(self):
        self.state.root.mkdir()
        for raw in (None, b"{}", b"not json"):
            if raw is not None:
                self.state.path.write_bytes(raw)
            self.assert_error(self.run_installer(), "image_validation_failed")
            self.assert_no_open()

    def test_state_file_and_directory_fsync_failure_fail_closed(self):
        self.state.load(self.validate())
        previous = self.state.load(self.validate())
        for fail_at in (1, 2):
            fresh = verified_state.VerifiedState(self.root / ("crash-" + str(fail_at)), "0.1.0")
            fresh.load(self.validate())
            original = verified_state.os.fsync
            calls = []

            def fsync(fd):
                calls.append(fd)
                if len(calls) == fail_at:
                    raise OSError("crash")
                original(fd)

            with patch.object(verified_state.os, "fsync", side_effect=fsync):
                with self.assertRaises(OSError):
                    fresh.intent(previous)
            with self.assertRaises(ValueError):
                fresh.load(self.validate())
            restarted = verified_state.VerifiedState(fresh.root, "original")
            if fail_at == 2:
                with self.assertRaises(ValueError):
                    restarted.load(self.validate())
            else:
                self.assertEqual(restarted.load(self.validate())["version"], "0.1.0")

    def test_coordinator_dynamic_journal_reload_and_deep_snapshot(self):
        worker = Mock(return_value=dict(status="error", error="cancelled"))
        jobs = ota_jobs.OtaJobs("pi-lab-01", self.root / "jobs", worker, clock=lambda: 1000)
        self.addCleanup(jobs.close)
        response = jobs.submit(self.command)
        jobs._worker.join()
        self.assertEqual(response["version"], "7.8.9")
        jobs.close()
        restarted = ota_jobs.OtaJobs("pi-lab-01", self.root / "jobs", worker, clock=lambda: 3000)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.submit(self.command)["status"], "error")
        worker.assert_called_once()
        snapshot, _ = ota_jobs.OtaJobs._command(self.command)
        snapshot["release"]["payload"] = "changed"
        self.assertNotEqual(snapshot["release"], self.command["release"])

    def test_legacy_install_also_advances_enabled_predecessor_state(self):
        self.command = dict(command_id="legacy", command="install_firmware", version="0.2.0",
                            from_version="0.1.0", sha256=rom_program.sha256(self.images["0.2.0"]), expires_at=2000)
        self.future_app = line("0.2.0") * 2
        self.assertEqual(self.run_installer()["status"], "application_verified")
        self.assertEqual(json.loads(self.state.path.read_text())["version"], "0.2.0")

    def test_client_signed_command_real_coordinator_handoff_installer_and_safe_ack(self):
        opened = threading.Event()
        release_read = threading.Event()
        events = []

        def read(count):
            opened.set()
            release_read.wait(3)
            return b""

        def open_monitor(device):
            events.append("monitor_open")
            return Mock(read=read, close=lambda: events.append("monitor_close"))

        monitor = uart_state.StateMonitor("/dev/ttyAMA0", opener=open_monitor)
        wrapped = uart_state.MonitoredInstaller(self.installer, monitor)

        def install(command, progress, stop):
            def report(stage, **details):
                self.assertIn("monitor_close", events)
                self.assertTrue(monitor.snapshot()["updating"])
                self.progress(stage, **details)
                progress(stage, **details)
            return wrapped(command, report, stop)

        jobs = ota_jobs.OtaJobs("pi-lab-01", self.root / "jobs", install, clock=lambda: 1000)
        jobs.monitor = monitor
        self.addCleanup(jobs.close)
        self.addCleanup(release_read.set)
        monitor.start()
        self.assertTrue(opened.wait(3))
        release_read.set()
        handler = client.CommandHandler("pi-lab-01", ota=jobs)
        raw = json.dumps(self.command).encode()
        accepted = handler.handle(raw, 0)
        self.assertEqual(accepted["status"], "accepted")
        jobs._worker.join()
        final = jobs.pending_results()[0]
        self.assertEqual(final["status"], "application_verified", final)
        self.assertTrue(final["flash_verified"])
        self.assertTrue(final["application_health_verified"])
        self.assertGreater(final["event_seq"], accepted["event_seq"])
        self.assertEqual(self.rom.commands.count(0x44), 1)
        self.assertEqual(bytes(self.rom.memory), self.full_flash(self.image))
        self.assertTrue(jobs.mark_delivered(final["command_id"], final["event_seq"]))
        self.assertEqual(handler.handle(raw, 0), final)
        self.assertEqual(self.rom.commands.count(0x44), 1)
        jobs.close()
        self.assertEqual(events, ["monitor_open", "monitor_close", "monitor_open", "monitor_close"])
        public = json.dumps(final)
        journal = next(jobs.state_root.glob("*.json")).read_text()
        for forbidden in (self.command["url"], self.command["release"]["payload"],
                          "BOOT0", "B2", "run_directory", "signature"):
            self.assertNotIn(forbidden, public)
            self.assertNotIn(forbidden, journal)

    def test_exact_20k_image_uses_only_ten_pages_and_preserves_original_tail(self):
        self.image += b"\xff" * (20480 - len(self.image))
        self.metadata = payload("7.8.9", self.image)
        self.command.update(sha256=self.metadata["sha256"], release=envelope(self.key, self.metadata))
        self.assertEqual(self.run_installer()["status"], "application_verified")
        self.assertEqual(self.rom.erased_pages, list(range(10)))
        self.assertEqual(sum(self.rom.write_sizes), 20480)
        self.assertEqual(bytes(self.rom.memory[20480:]), self.baseline[20480:])

    def test_signed_expiry_and_cancel_after_download_never_open_uart(self):
        original = self.installer._download

        def download(*args):
            selected = original(*args)
            self.clock.now = 1000
            return selected

        with patch.object(self.installer, "_download", side_effect=download):
            self.assert_error(self.run_installer(), "command_expired")
        self.assert_no_open()
        self.clock.now = 0
        self.stop.set()
        self.opener.open.reset_mock()
        self.assert_error(self.run_installer(), "cancelled")
        self.opener.open.assert_not_called()

    def test_changed_original_tail_file_refused_before_download(self):
        self.baseline_path.write_bytes(self.baseline[:-1] + bytes([self.baseline[-1] ^ 1]))
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.opener.open.assert_not_called()
        self.assert_no_open()