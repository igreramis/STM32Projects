"""Software only: synthetic pinned catalog, independent ROM emulator, fake clocks.

No physical UART, network, deployment, credentials, or production catalog needed.
All serial constructors are replaced. Downloads exercise the actual stager with
an in-memory HTTPS response, never a live opener.
"""

import contextlib
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import urlencode

import firmware
import heartbeat_monitor as heartbeat
import ota_installer as ota
import rom_local
import rom_program as engine
import test_rom_local as fixtures
from test_rom_program import MemoryRom


SECRET = "https://secret.invalid/image?X-Amz-Credential=PRIVATE&X-Amz-Signature=SECRET"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def line(version="0.2.0"):
    return heartbeat.PREFIX + version.encode("ascii") + b"\r\n"


class SyncPort:
    def __init__(self, clock, ready=None, response=b"\x79"):
        self.clock = clock
        self.ready = ready
        self.response = response
        self.pending = bytearray(b"stale application bytes")
        self.writes = []
        self.write_times = []
        self.read_timeouts = []
        self.write_timeouts = []
        self.clears = 0
        self.timeout = self.write_timeout = 0
        self.write_duration = 0

    def reset_input_buffer(self):
        self.clears += 1
        self.pending.clear()

    def write(self, data):
        self.writes.append(data)
        self.write_times.append(self.clock())
        self.write_timeouts.append(self.write_timeout)
        self.clock.now += min(self.write_duration, self.write_timeout)
        if self.ready is not None and self.clock() >= self.ready:
            self.pending.extend(self.response)
        return len(data)

    def read(self, count):
        self.read_timeouts.append(self.timeout)
        data = bytes(self.pending[:count])
        del self.pending[:count]
        if not data:
            self.clock.now += self.timeout
        return data


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stop = threading.Event()
        self.port = SyncPort(self.clock)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def wait(self):
        return ota.wait_for_rom(self.port, self.clock, self.stop)

    def assert_code(self, code):
        with self.assertRaises(ota.InstallerError) as caught:
            self.wait()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_delayed_operator_only_sync_and_single_clear(self):
        self.port.ready = 8.0
        self.wait()
        self.assertEqual(self.clock(), 8.0)
        self.assertEqual(self.port.write_times, [0, 2, 4, 6, 8])
        self.assertEqual(self.port.writes, [b"\x7f"] * 5)
        self.assertEqual(self.port.clears, 1)
        self.assertIn("BOOT0 HIGH", self.output.getvalue())
        self.assertIn("press/release B2", self.output.getvalue())

    def test_no_ack_120_second_deadline_and_60_attempts(self):
        self.assert_code("bootloader_timeout")
        self.assertEqual(self.clock(), 120)
        self.assertEqual(len(self.port.writes), 60)
        self.assertEqual(self.port.write_times[-1], 118)
        self.assertTrue(all(0 <= t <= 0.25 for t in self.port.read_timeouts))
        self.assertTrue(all(0 < t <= 2 for t in self.port.write_timeouts))

    def test_attempt_cap_even_if_deadline_longer(self):
        with patch.object(ota, "ROM_WAIT_SECONDS", 1000):
            self.assert_code("bootloader_timeout")
        self.assertEqual(len(self.port.writes), 60)
        self.assertEqual(self.clock(), 120)

    def test_remaining_deadline_caps_write_and_read_timeouts(self):
        with patch.object(ota, "ROM_WAIT_SECONDS", 2.1):
            self.assert_code("bootloader_timeout")
        self.assertAlmostEqual(self.port.write_timeouts[-1], 0.1)
        self.assertAlmostEqual(self.clock(), 2.1)
        self.assertLessEqual(max(self.port.read_timeouts), 0.25)

    def test_write_time_included_in_two_second_spacing(self):
        self.port.ready = 5
        self.port.write_duration = 0.75
        self.wait()
        self.assertEqual(self.port.write_times, [0, 2, 4, 6])
        self.assertEqual(self.clock(), 6.75)

    def test_bounded_ordinary_noise_before_ack(self):
        self.port.ready = 0
        self.port.response = line("0.1.0") * 3 + b"\x79"
        self.wait()
        self.assertEqual(self.port.writes, [b"\x7f"])
        self.assertEqual(self.port.pending, b"")

    def test_noise_flood_fails_even_if_clock_does_not_advance(self):
        self.port.ready = 0
        self.port.response = b"x" * (ota.ROM_NOISE_LIMIT + 1) + b"\x79"
        self.assert_code("bootloader_sync_failed")
        self.assertEqual(self.port.writes, [b"\x7f"])

    def test_state_json_letter_y_is_not_rom_ack(self):
        self.port.ready = 0
        self.port.response = b'{"type":"state","state":"ready"}\r\n' + b"\x79"
        self.wait()
        self.assertEqual(self.port.pending, b"")
        self.assertEqual(self.port.writes, [b"\x7f"])

    def test_state_json_without_rom_ack_never_identifies(self):
        self.port.ready = 0
        self.port.response = b'{"type":"state","state":"ready"}\r\n'
        self.assert_code("bootloader_timeout")

    def test_ack_inside_truncated_json_fails_closed(self):
        self.port.ready = 0
        self.port.response = b'{"type":"sta' + b"\x79\x1f"
        self.assert_code("bootloader_sync_failed")
        self.assertEqual(self.port.writes, [b"\x7f"])

    def test_nack_stops_without_retry_or_identification(self):
        self.port.ready = 0
        self.port.response = b"noise\x1f\x79"
        self.assert_code("bootloader_sync_failed")
        self.assertEqual(self.port.writes, [b"\x7f"])
        self.assertEqual(self.port.pending, b"\x79")

    def test_late_ack_before_next_probe_not_flushed_or_retransmitted(self):
        original = self.port.read

        def read(count):
            value = original(count)
            if not value and self.clock() == 2:
                self.port.pending.extend(b"noise\x79")
            return value

        self.port.read = read
        self.wait()
        self.assertEqual(self.port.writes, [b"\x7f"])
        self.assertEqual(self.port.clears, 1)

    def test_short_sync_write_requires_reset_no_retry(self):
        self.port.write = Mock(return_value=0)
        self.assert_code("bootloader_sync_failed")
        self.port.write.assert_called_once_with(b"\x7f")

    def test_cancel_before_sync_never_clears_or_writes(self):
        self.stop.set()
        self.assert_code("cancelled")
        self.assertEqual(self.port.writes, [])
        self.assertEqual(self.port.clears, 0)

    def test_cancel_during_wait_checked_on_read(self):
        original = self.port.read

        def read(count):
            value = original(count)
            self.stop.set()
            return value

        self.port.read = read
        self.assert_code("cancelled")
        self.assertEqual(self.clock(), 0.25)

    def test_transport_exception_is_sanitized(self):
        self.port.read = Mock(side_effect=OSError(SECRET))
        self.assert_code("bootloader_sync_failed")
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_ack_after_global_deadline_is_not_success(self):
        def read(count):
            self.clock.now = 121
            return b"\x79"

        self.port.read = read
        self.assert_code("bootloader_timeout")
        self.assertEqual(self.port.writes, [b"\x7f"])


class WorkerRom(MemoryRom):
    def __init__(self, clock, timeline):
        super().__init__()
        self.clock = clock
        self.timeline = timeline
        self.ready = 0
        self.clears = 0
        self.opened = self.closed = False

    def open(self):
        assert self.dtr is False and self.rts is False
        self.opened = True
        self.timeline.append("rom_open")

    def close(self):
        self.closed = True
        self.timeline.append("rom_close")

    def reset_input_buffer(self):
        self.clears += 1
        self.reply.clear()
        self.timeline.append("rom_clear")

    def write(self, data):
        if self.stage == "sync" and self.clock() < self.ready:
            assert data == b"\x7f"
            self.packets.append(bytes(data))
            return len(data)
        if self.stage == "command" and data == b"\x44\xbb":
            self.timeline.append("erase_command")
        return super().write(data)

    def read(self, count):
        value = super().read(count)
        if not value:
            self.clock.now += self.timeout
        return value


class AppPort:
    def __init__(self, clock, timeline):
        self.clock = clock
        self.timeline = timeline
        self.reply = bytearray(line() * 2)  # Stale matches MUST be discarded.
        self.opened = self.closed = False
        self.clears = 0
        self.timeout = 0.25
        self.write = Mock(side_effect=AssertionError("app UART must be receive-only"))

    def open(self):
        assert self.dtr is False and self.rts is False
        self.opened = True
        self.timeline.append("app_open")

    def close(self):
        self.closed = True
        self.timeline.append("app_close")

    def reset_input_buffer(self):
        self.clears += 1
        self.reply.clear()
        self.timeline.append("app_clear")

    def read(self, count):
        assert count == 1
        value = bytes(self.reply[:count])
        del self.reply[:count]
        self.clock.now += 0.001 if value else self.timeout
        return value


class InstallerFixture(fixtures.Fixture):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.timeline = []
        self.rom = WorkerRom(self.clock, self.timeline)
        self.rom.memory = bytearray(self.full_flash(self.images["0.1.0"]))
        self.app = AppPort(self.clock, self.timeline)
        self.addCleanup(self.app.write.assert_not_called)
        self.stop = threading.Event()
        self.installer = ota.Installer(self.catalog_path, self.release_root, self.baseline_path,
                                       self.root / "ota-runs", port="/dev/ttyAMA0")
        self.installer.clock = self.clock
        self.installer.wall_clock = lambda: 1000 + self.clock()
        self.command = dict(command_id="job-1", command="install_firmware", version="0.2.0",
                            from_version="0.1.0", sha256=fixtures.digest(self.images["0.2.0"]),
                            expires_at=2000)
        self.events = []
        self.future_app = line() * 2
        self.on_progress = None

        def construct(**kwargs):
            port = self.rom if kwargs["parity"] == "E" else self.app
            for key, value in kwargs.items():
                setattr(port, key, value)
            return port

        self.serial = SimpleNamespace(Serial=Mock(side_effect=construct), EIGHTBITS=8,
                                      STOPBITS_ONE=1, PARITY_EVEN="E", PARITY_NONE="N")
        serial_patch = patch.dict("sys.modules", {"serial": self.serial})
        serial_patch.start()
        self.addCleanup(serial_patch.stop)
        # Default fail-closed network guard for EVERY installer test.
        self.opener = Mock()
        self.opener.open.side_effect = AssertionError("network is forbidden")
        network_patch = patch.object(firmware, "build_opener", return_value=self.opener)
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def progress(self, stage, **details):
        self.timeline.append(stage)
        self.events.append((stage, details))
        if self.on_progress is not None:
            self.on_progress(stage, details)
        if stage == "waiting_for_application":
            self.app.reply.extend(self.future_app)

    def run_installer(self):
        with patch("builtins.input", side_effect=AssertionError("no interactive input")):
            result = self.installer(self.command, self.progress, self.stop)
        self.assertEqual(self.globals(), self.original_globals)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(self.events))
        self.assertNotIn(SECRET, self.output.getvalue())
        return result

    def assert_error(self, result, code, flashed=False):
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["error"], code)
        self.assertIs(result["flashed"], flashed)
        self.assertEqual(result["flash_verified"], flashed is True)

    def assert_no_open(self):
        self.serial.Serial.assert_not_called()
        self.assertEqual(self.rom.packets, [])

    def download_config(self):
        release = firmware.Release(
            target="NUCLEO-G0B1RE", version="0.2.0", size_bytes=len(self.images["0.2.0"]),
            sha256=fixtures.digest(self.images["0.2.0"]), bucket="iot-lab-firmware-844274146091",
            region="ap-southeast-2", key="firmware/stm32g0b1/0.2.0/firmware.bin",
            version_id="TEST-ONLY-SECRET-VERSION-ID")
        metadata = self.root / "approved-release.json"
        metadata.write_text(json.dumps(asdict(release)))
        self.installer.download_releases = {"0.2.0": str(metadata)}
        query = urlencode({"versionId": release.version_id, "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                           "X-Amz-Signature": "a" * 64})
        self.command["url"] = "https://{}/{}?{}".format(release.host, release.key, query)
        return release, metadata


class InstallerTests(InstallerFixture):
    def test_get_failure_diagnostic_is_durable_and_local_only(self):
        original = self.rom.write

        def write(data):
            result = original(data)
            if data == b"\x00\xff":
                self.rom.reply[:] = b"\x1f"
            return result

        self.rom.write = write
        result = self.run_installer()
        self.assert_error(result, "target_validation_failed")
        self.assertFalse(result["flash_may_be_modified"])
        self.assertEqual(self.rom.packets, [b"\x7f", b"\x00\xff"])
        self.assertTrue(self.rom.closed)
        self.assertFalse(self.app.opened)
        path = Path(result["run_directory"]) / "events.jsonl"
        entries = [json.loads(raw) for raw in path.read_text().splitlines()]
        diagnostic = [entry for entry in entries if entry["event"] == "rom-probe-failed"]
        self.assertEqual(len(diagnostic), 1)
        self.assertEqual(diagnostic[0]["stage"], "command 0x00")
        self.assertEqual(diagnostic[0]["reason"], "nack")
        self.assertEqual(entries[-1]["event"], "failed")
        self.assertNotIn("rom-probe-failed", json.dumps(self.events))
        self.assertNotIn("reason", result)

    def test_non_pl011_port_refused_before_open(self):
        self.installer.port = "/dev/ttyS0"
        self.assert_error(self.run_installer(), "bootloader_sync_failed")
        self.assert_no_open()

    def test_full_engine_known_predecessor_preserved_tail_and_stable_api(self):
        result = self.run_installer()
        self.assertEqual(result, dict(
            status="application_verified", flashed=True, flash_verified=True,
            flash_may_be_modified=True, application_health_verified=True, version="0.2.0",
            image_sha256=fixtures.digest(self.images["0.2.0"]),
            flash_sha256=fixtures.digest(self.full_flash(self.images["0.2.0"])),
            run_directory=result["run_directory"]))
        self.assertEqual(bytes(self.rom.memory), self.full_flash(self.images["0.2.0"]))
        self.assertEqual(self.rom.erased_pages, list(range(10)))
        self.assertEqual(self.rom.commands.count(0x44), 1)
        self.assertEqual(self.rom.commands[:3], [0, 1, 2])
        self.assertEqual(self.rom.packets.count(b"\x7f"), 1)
        self.assertEqual(sum(self.rom.write_sizes), 48)
        directory = Path(result["run_directory"])
        self.assertEqual((directory / "flash-before.bin").read_bytes(),
                         self.full_flash(self.images["0.1.0"]))
        self.assertEqual((directory / "flash-after.bin").read_bytes(), bytes(self.rom.memory))
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        events = [json.loads(raw) for raw in (directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "verified")

    def test_rom_close_before_app_8n1_open_clear_before_boot_notice(self):
        self.run_installer()
        for first, second in (("rom_close", "app_open"), ("app_open", "app_clear"),
                              ("app_clear", "flash_verified"), ("flash_verified", "waiting_for_application")):
            self.assertLess(self.timeline.index(first), self.timeline.index(second))
        configs = [call.kwargs for call in self.serial.Serial.call_args_list]
        self.assertEqual([c["parity"] for c in configs], ["E", "N"])
        for config in configs:
            self.assertEqual(config["baudrate"], 115200)
            self.assertEqual(config["bytesize"], 8)
            self.assertEqual(config["stopbits"], 1)
            self.assertTrue(config["exclusive"])
        self.assertTrue(self.rom.closed and self.app.closed)
        self.assertEqual(self.rom.clears, 1)
        self.assertEqual(self.app.clears, 1)

    def test_delayed_rom_operator_full_engine_no_double_sync(self):
        self.rom.ready = 10
        self.assertEqual(self.run_installer()["status"], "application_verified")
        self.assertEqual(self.rom.packets.count(b"\x7f"), 6)
        self.assertEqual(self.rom.commands.count(0), 1)
        self.assertEqual(self.rom.commands.count(1), 1)
        self.assertEqual(self.rom.commands.count(2), 1)

    def test_original_predecessor_supported_explicitly(self):
        self.command["from_version"] = "original"
        self.rom.memory = bytearray(self.baseline)
        self.assertEqual(self.run_installer()["status"], "application_verified")

    def test_automatic_confirmation_only_after_verified_backup(self):
        original = engine.Programmer.run
        confirmations = []

        def run(programmer, confirm):
            with self.assertRaises(ota.InstallerError) as caught:
                confirm("early")
            self.assertEqual(caught.exception.code, "backup_failed")

            def check(prompt):
                self.assertEqual(programmer.phase, "backed-up")
                backup = programmer.record.path / "flash-before.bin"
                self.assertEqual(backup.read_bytes(), self.full_flash(self.images["0.1.0"]))
                value = confirm(prompt)
                self.assertEqual(value, engine.CONFIRMATION)
                confirmations.append(value)
                return value

            return original(programmer, check)

        with patch.object(engine.Programmer, "run", run):
            self.assertEqual(self.run_installer()["status"], "application_verified")
        self.assertEqual(len(confirmations), 1)

    def test_bad_command_fields_predecessor_hash_expiry_no_open(self):
        original = dict(self.command)
        commands = [dict(original, from_version="unknown"), dict(original, sha256="0" * 64),
                    dict(original, expires_at=1000), dict(original, expires_at=True),
                    dict(original, expires_at=2000.0), dict(original, version="original"),
                    dict(original, command="stage_firmware"), dict(original, arbitrary=1),
                    dict(original, command_id=SECRET)]
        for field in original:
            commands.append({key: value for key, value in original.items() if key != field})
        for command in commands:
            with self.subTest(fields=list(command)):
                self.command = command
                code = "command_expired" if command.get("expires_at") == 1000 else "image_validation_failed"
                self.assert_error(self.run_installer(), code)
                self.assert_no_open()

    def test_cancelled_before_validation_never_opens(self):
        self.stop.set()
        self.assert_error(self.run_installer(), "cancelled")
        self.assert_no_open()

    def test_bad_baseline_no_open(self):
        self.baseline_path.write_bytes(b"wrong")
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_bad_unselected_image_no_open(self):
        (self.release_root / "0.3.0" / "firmware.bin").write_bytes(b"wrong")
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_invalid_full_flash_hash_no_open(self):
        self.catalog["releases"]["0.3.0"]["expected_flash_sha256"] = "0" * 64
        self.save_catalog()
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_catalog_cannot_override_baseline_pin(self):
        self.catalog["baseline_sha256"] = "0" * 64
        self.save_catalog()
        self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_selected_image_revalidated_after_catalog(self):
        original = rom_local.validate_catalog

        def change(*args):
            releases = original(*args)
            releases["0.2.0"].path.write_bytes(b"changed")
            return releases

        with patch.object(rom_local, "validate_catalog", side_effect=change):
            self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_immutable_image_and_command_before_serial(self):
        def mutate(stage, details):
            if stage == "image_validated":
                (self.release_root / "0.2.0" / "firmware.bin").write_bytes(b"changed")
                self.command.update(version="0.3.0", expires_at=0)

        self.on_progress = mutate
        self.assertEqual(self.run_installer()["status"], "application_verified")
        self.assertEqual(bytes(self.rom.memory), self.full_flash(self.images["0.2.0"]))

    def test_expiry_after_validation_before_serial_open(self):
        def expire(stage, details):
            if stage == "image_validated":
                self.clock.now = 1000

        self.on_progress = expire
        self.assert_error(self.run_installer(), "command_expired")
        self.assert_no_open()

    def test_expiry_at_erase_after_confirmation_stops_command(self):
        original = engine.Programmer._erase

        def erase(programmer):
            self.clock.now = 1000
            return original(programmer)

        with patch.object(engine.Programmer, "_erase", erase):
            self.assert_error(self.run_installer(), "command_expired")
        self.assertNotIn(0x44, self.rom.commands)
        self.assertTrue(self.rom.closed)
        self.assertFalse(self.app.opened)

    def test_expiry_during_durable_erase_callback_stops_command(self):
        def expire(stage, details):
            if stage == "erase_started":
                self.clock.now = 1000

        self.on_progress = expire
        self.assert_error(self.run_installer(), "command_expired")
        self.assertNotIn(0x44, self.rom.commands)

    def test_cancel_at_erase_gate_blocks_command(self):
        def cancel(stage, details):
            if stage == "erase_started":
                self.stop.set()

        self.on_progress = cancel
        self.assert_error(self.run_installer(), "cancelled")
        self.assertNotIn(0x44, self.rom.commands)

    def test_erase_callback_precedes_command_and_has_conservative_flag(self):
        def inspect(stage, details):
            if stage == "erase_started":
                self.assertTrue(details["flash_may_be_modified"])
                self.assertNotIn(0x44, self.rom.commands)
                record = Path(details["run_directory"])
                entries = [json.loads(raw) for raw in (record / "events.jsonl").read_text().splitlines()]
                self.assertEqual(entries[-1]["event"], "erase-intent")

        self.on_progress = inspect
        self.run_installer()
        self.assertLess(self.timeline.index("erase_started"), self.timeline.index("erase_command"))

    def test_callback_storage_failure_before_erase_blocks_command(self):
        def fail(stage, details):
            if stage == "erase_started":
                raise OSError(SECRET)

        self.on_progress = fail
        result = self.run_installer()
        self.assert_error(result, "programming_failed")
        self.assertFalse(result["flash_may_be_modified"])
        self.assertNotIn(0x44, self.rom.commands)
        self.assertFalse(self.app.opened)

    def test_backup_storage_failure_no_erase(self):
        with patch.object(engine.RunRecord, "snapshot", side_effect=OSError(SECRET)):
            self.assert_error(self.run_installer(), "backup_failed")
        self.assertNotIn(0x44, self.rom.commands)

    def test_intent_fsync_failure_no_erase(self):
        original = engine.RunRecord.event

        def fail(record, name, **details):
            if name == "erase-intent":
                raise OSError(SECRET)
            return original(record, name, **details)

        with patch.object(engine.RunRecord, "event", fail):
            self.assert_error(self.run_installer(), "programming_failed")
        self.assertNotIn(0x44, self.rom.commands)

    def test_wrong_predecessor_backup_no_confirmation_or_erase(self):
        self.rom.memory = bytearray(self.baseline)
        result = self.run_installer()
        self.assert_error(result, "backup_failed")
        self.assertNotIn("erase_started", self.timeline)
        self.assertNotIn(0x44, self.rom.commands)

    def test_wrong_target_no_identification_retry_or_app_open(self):
        self.rom.pid = 0x466
        self.assert_error(self.run_installer(), "target_validation_failed")
        self.assertEqual(self.rom.commands, [0, 1, 2])
        self.assertFalse(self.app.opened)

    def test_wrong_geometry_no_erase(self):
        self.rom.optr ^= 1 << 21
        self.assert_error(self.run_installer(), "target_validation_failed")
        self.assertNotIn(0x44, self.rom.commands)

    def test_sync_nack_reports_reset_required_without_identification(self):
        self.rom.fail_stage = "sync"
        self.assert_error(self.run_installer(), "bootloader_sync_failed")
        self.assertEqual(self.rom.commands, [])
        self.assertIn("manual reset required", self.output.getvalue())

    def test_no_ack_deadline_closes_rom_without_app_open(self):
        self.rom.ready = 10000
        self.assert_error(self.run_installer(), "bootloader_timeout")
        self.assertEqual(self.clock(), 120)
        self.assertEqual(len(self.rom.packets), 60)
        self.assertTrue(self.rom.closed)
        self.assertFalse(self.app.opened)

    def test_failed_erase_is_uncertain_no_retry_no_boot(self):
        self.rom.fail_stage = "erase-pages"
        result = self.run_installer()
        self.assert_error(result, "programming_failed", flashed=None)
        self.assertTrue(result["flash_may_be_modified"])
        self.assertEqual(self.rom.commands.count(0x44), 1)
        self.assertNotIn(0x31, self.rom.commands)
        self.assertFalse(self.app.opened)
        self.assertNotIn("BOOT0 LOW", self.output.getvalue())

    def test_failed_write_is_uncertain_no_retransmission(self):
        self.rom.fail_stage = "write-data"
        self.assert_error(self.run_installer(), "programming_failed", flashed=None)
        self.assertEqual(self.rom.commands.count(0x31), 1)
        self.assertFalse(self.app.opened)

    def test_readback_mismatch_uncertain_no_application_open(self):
        self.rom.corrupt_write = True
        self.assert_error(self.run_installer(), "verification_failed", flashed=None)
        self.assertFalse(self.app.opened)

    def test_readback_snapshot_failure_uncertain(self):
        original = engine.RunRecord.snapshot

        def fail(record, name, data):
            if name == "flash-after.bin":
                raise OSError(SECRET)
            return original(record, name, data)

        with patch.object(engine.RunRecord, "snapshot", fail):
            self.assert_error(self.run_installer(), "verification_failed", flashed=None)
        self.assertFalse(self.app.opened)

    def test_verified_journal_failure_retains_evidence_without_app_open(self):
        original = engine.RunRecord.event

        def fail(record, name, **details):
            if name == "verified":
                raise OSError(SECRET)
            return original(record, name, **details)

        with patch.object(engine.RunRecord, "event", fail):
            self.assert_error(self.run_installer(), "verification_failed", flashed=True)
        self.assertFalse(self.app.opened)

    def test_heartbeat_timeout_stale_matches_not_counted_flash_stays_true(self):
        self.future_app = b""
        result = self.run_installer()
        self.assert_error(result, "application_start_timeout", flashed=True)
        self.assertFalse(result["application_health_verified"])
        self.assertEqual(self.clock(), 120)
        self.assertTrue(self.app.closed)

    def test_only_one_fresh_match_is_not_application_verified(self):
        self.future_app = line()
        self.assert_error(self.run_installer(), "application_start_timeout", flashed=True)

    def test_wrong_version_and_secret_noise_not_counted_or_logged(self):
        self.future_app = SECRET.encode() + b"\n" + line("0.3.0") * 2 + line()
        self.assert_error(self.run_installer(), "application_start_timeout", flashed=True)
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_two_exact_matches_allow_noise_but_not_trailing_space(self):
        self.future_app = b"noise\n" + line().replace(b"\r\n", b" \n") + line() * 2
        self.assertEqual(self.run_installer()["status"], "application_verified")

    def test_app_line_bound_512(self):
        self.future_app = b"x" * 513 + b"\n" + line() * 2
        self.assert_error(self.run_installer(), "application_start_timeout", flashed=True)
        self.assertLess(self.clock(), 1)

    def test_app_cancel_during_read_retains_flash_verified(self):
        original = self.app.read

        def read(count):
            self.stop.set()
            return original(count)

        self.app.read = read
        self.assert_error(self.run_installer(), "cancelled", flashed=True)
        self.assertTrue(self.app.closed)

    def test_serial_open_error_sanitized_and_closed(self):
        self.rom.open = Mock(side_effect=OSError(SECRET))
        self.assert_error(self.run_installer(), "bootloader_sync_failed")
        self.assertTrue(self.rom.closed)
        self.assertFalse(self.app.opened)

    def test_app_open_error_retains_flash_verified(self):
        self.app.open = Mock(side_effect=OSError(SECRET))
        self.assert_error(self.run_installer(), "application_start_timeout", flashed=True)
        self.assertTrue(self.rom.closed and self.app.closed)
        self.assertNotIn("BOOT0 LOW", self.output.getvalue())

    def test_app_clear_failure_no_boot_notice(self):
        self.app.reset_input_buffer = Mock(side_effect=OSError(SECRET))
        self.assert_error(self.run_installer(), "application_start_timeout", flashed=True)
        self.assertNotIn("BOOT0 LOW", self.output.getvalue())

    def test_rom_close_failure_prevents_app_open(self):
        self.rom.close = Mock(side_effect=OSError(SECRET))
        self.assert_error(self.run_installer(), "verification_failed", flashed=True)
        self.assertFalse(self.app.opened)

    def test_globals_restored_after_keyboard_interrupt(self):
        self.rom.write = Mock(side_effect=KeyboardInterrupt)
        self.assert_error(self.run_installer(), "cancelled")
        self.assertTrue(self.rom.closed)

    def test_progress_failure_after_flash_keeps_verified_flags(self):
        def fail(stage, details):
            if stage == "flash_verified":
                raise OSError(SECRET)

        self.on_progress = fail
        self.assert_error(self.run_installer(), "application_start_timeout", flashed=True)
        self.assertNotIn("BOOT0 LOW", self.output.getvalue())

    def test_post_erase_progress_never_clears_uncertain_flag(self):
        self.assertEqual(self.run_installer()["status"], "application_verified")
        started = False
        for stage, details in self.events:
            started = started or stage == "erase_started"
            if started:
                self.assertTrue(details["flash_may_be_modified"], stage)
            if stage in ("programming", "verifying_flash"):
                self.assertIsNone(details["flashed"])
                self.assertFalse(details["flash_verified"])

    def test_expiry_after_backup_blocks_auto_confirmation(self):
        def expire(stage, details):
            if stage == "backup_saved":
                self.clock.now = 1000

        self.on_progress = expire
        self.assert_error(self.run_installer(), "command_expired")
        self.assertNotIn("erase_started", self.timeline)
        self.assertNotIn(0x44, self.rom.commands)

    def test_stop_after_erase_finishes_readback_but_never_opens_app(self):
        def cancel(stage, details):
            if stage == "programming":
                self.stop.set()

        self.on_progress = cancel
        self.assert_error(self.run_installer(), "cancelled", flashed=True)
        self.assertEqual(bytes(self.rom.memory), self.full_flash(self.images["0.2.0"]))
        self.assertFalse(self.app.opened)


class DownloadTests(InstallerFixture):
    def test_url_requires_explicit_local_metadata_no_open_or_network(self):
        self.command["url"] = SECRET
        self.assert_error(self.run_installer(), "download_not_approved")
        self.assert_no_open()
        self.opener.open.assert_not_called()

    def test_relative_metadata_path_rejected(self):
        self.command["url"] = SECRET
        self.installer.download_releases = {"0.2.0": "release.json"}
        self.assert_error(self.run_installer(), "download_not_approved")
        self.assert_no_open()

    def test_metadata_version_size_hash_must_match_catalog(self):
        release, path = self.download_config()
        for changed in (replace(release, size_bytes=release.size_bytes + 1),
                        replace(release, sha256="0" * 64),
                        replace(release, version="0.3.0", key="firmware/stm32g0b1/0.3.0/firmware.bin")):
            with self.subTest(version=changed.version, size=changed.size_bytes):
                path.write_text(json.dumps(asdict(changed)))
                self.assert_error(self.run_installer(), "download_not_approved")
                self.assert_no_open()
        self.opener.open.assert_not_called()

    def test_actual_stager_dedicated_job_directory_original_slot_untouched(self):
        release, _ = self.download_config()
        old_slot = self.root / "staged" / "firmware.bin"
        old_slot.parent.mkdir()
        old_slot.write_bytes(b"original-staged-slot")
        response = io.BytesIO(self.images["0.2.0"])
        response.status = 200
        response.headers = {"Content-Length": str(release.size_bytes),
                            "x-amz-version-id": release.version_id}
        self.opener.open.side_effect = None
        self.opener.open.return_value = response
        result = self.run_installer()
        self.assertEqual(result["status"], "application_verified")
        staged = Path(result["run_directory"]) / "download" / "firmware.bin"
        self.assertEqual(staged.read_bytes(), self.images["0.2.0"])
        self.assertEqual(old_slot.read_bytes(), b"original-staged-slot")
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.opener.open.assert_called_once()
        self.assertNotIn(self.command["url"], self.output.getvalue())
        self.assertNotIn(release.version_id, self.output.getvalue())
        self.assertNotIn(self.command["url"], json.dumps(self.events))
        self.assertNotIn(self.command["url"], (Path(result["run_directory"]) / "events.jsonl").read_text())

    def test_url_validation_uses_existing_stager_before_network(self):
        self.download_config()
        self.command["url"] = SECRET
        self.assert_error(self.run_installer(), "download_validation_failed")
        self.assert_no_open()
        self.opener.open.assert_not_called()

    def test_failed_download_redacted_no_open(self):
        self.download_config()
        self.opener.open.side_effect = URLError(SECRET)
        self.assert_error(self.run_installer(), "download_failed")
        self.assert_no_open()

    def test_unexpected_stage_error_cannot_leak_url(self):
        self.download_config()
        with patch.object(firmware.FirmwareStager, "stage", side_effect=firmware.StageError(SECRET)):
            self.assert_error(self.run_installer(), "download_failed")
        self.assert_no_open()

    def test_download_deadline_code(self):
        self.download_config()
        with patch.object(firmware.FirmwareStager, "stage",
                          side_effect=firmware.StageError("download_deadline_exceeded")):
            self.assert_error(self.run_installer(), "download_deadline_exceeded")
        self.assert_no_open()

    def test_staged_file_revalidated_not_trusting_returned_path(self):
        self.download_config()
        with patch.object(firmware.FirmwareStager, "stage", return_value={
                "staged_file": str(self.release_root / "0.2.0" / "firmware.bin")}):
            self.assert_error(self.run_installer(), "image_validation_failed")
        self.assert_no_open()

    def test_local_job_does_not_touch_download_metadata(self):
        self.installer.download_releases = {"0.2.0": SECRET}
        with patch.object(firmware.Release, "load") as load:
            self.assertEqual(self.run_installer()["status"], "application_verified")
        load.assert_not_called()
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()