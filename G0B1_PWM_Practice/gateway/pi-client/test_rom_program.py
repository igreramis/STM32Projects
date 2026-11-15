"""Software-only tests. No hardware tty is opened; PTY transport uses 8N1."""

import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import rom_program as program
from rom_read_id import ProbeError, READ_ONLY_COMMANDS


IMAGE = ((0x20024000).to_bytes(4, "little") + (0x08004935).to_bytes(4, "little") +
         bytes(i % 251 for i in range(program.IMAGE_SIZE - 8)))
TAIL = b"preserve-this!!!\x00"
BEFORE = IMAGE + b"\xff" * (program.FLASH_SIZE - len(IMAGE) - len(TAIL)) + TAIL
COMMANDS = bytes.fromhex("00 01 02 11 21 31 44 63 73 82 92")


class MemoryRom:
    """Independent packet decoder simulating the tested ROM capabilities."""

    def __init__(self):
        self.memory = bytearray(BEFORE)
        self.reply = bytearray()
        self.stage = "sync"
        self.command = None
        self.address = None
        self.commands = []
        self.packets = []
        self.write_sizes = []
        self.erased_pages = []
        self.version = 0x31
        self.pid = 0x0467
        self.capabilities = COMMANDS
        self.optr = program.EXPECTED_OPTR
        self.corrupt_option_complement = False
        self.size = 512
        self.fail_stage = None
        self.failure = b"\x1f"
        self.corrupt_erase = False
        self.corrupt_write = False
        self.change_outside = False
        self.fragment = 256
        self.timeout = 2
        self.write_timeout = 2

    def read(self, count):
        count = min(count, self.fragment)
        result = bytes(self.reply[:count])
        del self.reply[:count]
        return result

    def write(self, data):
        data = bytes(data)
        self.packets.append(data)
        stage = self.stage
        if stage == "command":
            if len(data) != 2 or data[1] != data[0] ^ 255:
                raise AssertionError("bad command/complement")
            self.command = data[0]
            self.commands.append(self.command)
            key = "cmd-{:02x}".format(self.command)
        else:
            key = stage
        if self.fail_stage == key:
            self.reply.extend(self.failure)
            return len(data)
        if stage == "sync":
            assert data == b"\x7f"
            self.reply.extend(b"\x79")
            self.stage = "command"
        elif stage == "command":
            self.reply.extend(b"\x79")
            if self.command == 0:
                self.reply.extend(bytes((len(self.capabilities), self.version)) + self.capabilities + b"\x79")
            elif self.command == 1:
                self.reply.extend(bytes((self.version, 0, 0, 0x79)))
            elif self.command == 2:
                self.reply.extend(b"\x01" + self.pid.to_bytes(2, "big") + b"\x79")
            elif self.command in (0x11, 0x31):
                self.stage = "read-address" if self.command == 0x11 else "write-address"
            elif self.command == 0x44:
                self.stage = "erase-pages"
            else:
                raise AssertionError("forbidden command {:02x}".format(self.command))
        elif stage in ("read-address", "write-address"):
            assert len(data) == 5 and self._xor(data) == 0
            self.address = int.from_bytes(data[:4], "big")
            self.reply.extend(b"\x79")
            self.stage = "read-length" if self.command == 0x11 else "write-data"
        elif stage == "read-length":
            assert len(data) == 2 and data[1] == data[0] ^ 255
            count = data[0] + 1
            if self.address == program.FLASH_SIZE_REGISTER:
                value = self.size.to_bytes(2, "little")
            elif self.address == program.OPTION_BYTES_ADDRESS:
                complement = self.optr ^ 0xFFFFFFFF
                if self.corrupt_option_complement:
                    complement ^= 1
                value = self.optr.to_bytes(4, "little") + complement.to_bytes(4, "little")
            else:
                offset = self.address - program.FLASH_BASE
                assert 0 <= offset and offset + count <= len(self.memory)
                value = bytes(self.memory[offset:offset + count])
            assert len(value) == count
            self.reply.extend(b"\x79" + value)
            self.stage = "command"
        elif stage == "erase-pages":
            assert self._xor(data) == 0
            count = int.from_bytes(data[:2], "big") + 1
            assert count == 10 and len(data) == 2 + 2 * count + 1
            self.erased_pages = [int.from_bytes(data[i:i + 2], "big") for i in range(2, len(data) - 1, 2)]
            assert self.erased_pages == list(range(10))
            for page in self.erased_pages:
                self.memory[page * 2048:(page + 1) * 2048] = b"\xff" * 2048
            if self.corrupt_erase:
                self.memory[0] = 0
            if self.change_outside:
                self.memory[-1] ^= 1
            self.reply.extend(b"\x79")
            self.stage = "command"
        elif stage == "write-data":
            assert self._xor(data) == 0 and len(data) == data[0] + 3
            payload = data[1:-1]
            assert len(payload) % 8 == 0 and self.address % 8 == 0
            offset = self.address - program.FLASH_BASE
            assert self.memory[offset:offset + len(payload)] == b"\xff" * len(payload)
            self.memory[offset:offset + len(payload)] = payload
            if self.corrupt_write:
                self.memory[offset] ^= 1
            self.write_sizes.append(len(payload))
            self.reply.extend(b"\x79")
            self.stage = "command"
        else:
            raise AssertionError("unexpected emulator stage")
        return len(data)

    @staticmethod
    def _xor(data):
        value = 0
        for byte in data:
            value ^= byte
        return value


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("IMAGE_SHA256", program.sha256(IMAGE)),
                            ("BASELINE_SHA256", program.sha256(BEFORE))):
            patcher = patch.object(program, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.port = MemoryRom()
        self.record = program.RunRecord(self.root / "runs")
        self.programmer = program.Programmer(self.port, IMAGE, self.record)

    def run_program(self, answer=program.CONFIRMATION):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.programmer.run(Mock(return_value=answer))


class ValidationTests(Fixture):
    def test_fixture_lengths_and_immutable_image(self):
        self.assertEqual(len(BEFORE), program.FLASH_SIZE)
        source = bytearray(IMAGE)
        p = program.Programmer(self.port, source, self.record)
        source[10] ^= 1
        self.assertEqual(p.image, IMAGE)
        self.assertEqual(p.padded[-4:], b"\xff" * 4)

    def test_wrong_image_size_and_hash(self):
        for data in (b"", IMAGE[:-1], IMAGE + b"x", IMAGE[:-1] + b"!"):
            with self.subTest(size=len(data)), self.assertRaises(ProbeError):
                program.validate_image(data)

    def test_vectors(self):
        for sp, reset in ((0x20000000, 0x08000009), (0x20024008, 0x08000009),
                          (0x20000001, 0x08000009), (0x20024000, 0x08000008),
                          (0x20024000, 0x08010001), (0x20024000, 0x07000001)):
            with self.subTest(sp=sp, reset=reset), self.assertRaises(ProbeError):
                program.validate_vectors(sp.to_bytes(4, "little") + reset.to_bytes(4, "little") + IMAGE[8:])
        with self.assertRaises(ProbeError):
            program.validate_vectors(b"short")

    def test_fixed_erase_packet(self):
        expected = bytes.fromhex("0009 0000 0001 0002 0003 0004 0005 0006 0007 0008 0009 08")
        self.assertEqual(program.erase_packet(range(10)), expected)
        for pages in ([], [65535], [65534], [65533], range(11), range(1, 11), list(range(9)) + [8], reversed(range(10))):
            with self.assertRaises(ProbeError):
                program.erase_packet(pages)

    def test_write_packet_and_bounds(self):
        self.assertEqual(program.write_packet(program.FLASH_BASE, bytes(range(8))),
                         bytes.fromhex("07 00 01 02 03 04 05 06 07 07"))
        for address, count in ((program.FLASH_BASE - 8, 8), (program.FLASH_BASE + 1, 8),
                               (program.FLASH_BASE, 0), (program.FLASH_BASE, 4),
                               (program.FLASH_BASE, 264), (program.FLASH_BASE + program.PADDED_SIZE, 8),
                               (0x1FFF7800, 8), (0x20000000, 8)):
            with self.subTest(address=address, count=count), self.assertRaises(ProbeError):
                program.write_packet(address, b"\xff" * count)

    def test_disallowed_commands_never_transmitted(self):
        for command in set(range(256)) - program.MEMORY_COMMANDS:
            with self.assertRaises(ProbeError):
                self.programmer._command(command, 100)
        self.assertEqual(self.port.packets, [])
        self.assertEqual(READ_ONLY_COMMANDS, frozenset((0, 1, 2)))

    def test_destructive_state_gates(self):
        for command in (0x31, 0x44):
            with self.assertRaises(ProbeError):
                self.programmer._command(command, 100)
        self.assertEqual(self.port.packets, [])

    def test_read_range_guard(self):
        for address, count in ((0, 1), (program.FLASH_BASE, 0), (program.FLASH_BASE, 257),
                               (program.FLASH_BASE + program.FLASH_SIZE - 1, 2),
                               (0x40022020, 4), (program.OPTION_BYTES_ADDRESS, 4), (0x20000000, 4)):
            with self.assertRaises(ProbeError):
                self.programmer.read_memory(address, count)
        self.assertEqual(self.port.packets, [])

    def test_out_of_order_or_changed_write_guard(self):
        self.programmer.phase = "erased-verified"
        for offset, data in ((8, IMAGE[8:16]), (0, b"changed!")):
            with self.assertRaises(ProbeError):
                self.programmer._write_block(offset, data)
        self.assertEqual(self.port.packets, [])


class WorkflowTests(Fixture):
    def test_diagnostic_mode_no_backup_or_confirmation(self):
        p = program.Programmer(self.port, IMAGE, self.record, diagnostic_only=True)
        confirm = Mock(side_effect=AssertionError("diagnosis must not ask to erase"))
        with contextlib.redirect_stdout(io.StringIO()):
            result = p.run(confirm)
        self.assertEqual(result["status"], "geometry_verified")
        self.assertFalse(result["flashed"])
        confirm.assert_not_called()
        self.assertEqual(self.port.commands, [0, 1, 2, 0x11, 0x11])
        self.assertFalse((self.record.path / "flash-before.bin").exists())

    def test_diagnostic_mode_blocks_destructive_even_with_phase(self):
        p = program.Programmer(self.port, IMAGE, self.record, diagnostic_only=True)
        for phase, command in (("erase-authorized", 0x44), ("erased-verified", 0x31)):
            p.phase = phase
            with self.assertRaisesRegex(ProbeError, "Read Memory only"):
                p._command(command, 100)
        self.assertEqual(self.port.packets, [])

    def test_geometry_nack_records_exact_address(self):
        for rejected in (program.FLASH_SIZE_REGISTER, program.OPTION_BYTES_ADDRESS):
            port = MemoryRom()
            original = port.write
            def reject(data):
                if port.stage == "read-address" and int.from_bytes(data[:4], "big") == rejected:
                    port.reply.extend(b"\x1f")
                    return len(data)
                return original(data)
            port.write = reject
            record = program.RunRecord(self.root / "diagnostic-runs")
            p = program.Programmer(port, IMAGE, record, diagnostic_only=True)
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaisesRegex(ProbeError, "0x{:08X}.*NACK".format(rejected)):
                p.run(Mock())
            events = [json.loads(line) for line in (record.path / "events.jsonl").read_text().splitlines()]
            starts = [event for event in events if event["event"] == "geometry-read-start"]
            self.assertEqual(starts[-1]["address"], hex(rejected))
            self.assertFalse(events[-1]["flash_may_be_modified"])

    def test_success_full_backup_readback_and_page_boundary(self):
        result = self.run_program()
        self.assertEqual(result["status"], "flash_verified")
        self.assertTrue(result["flashed"])
        self.assertFalse(result["application_health_verified"])
        self.assertEqual(self.port.erased_pages, list(range(10)))
        self.assertEqual(self.port.write_sizes, [256] * 75 + [16])
        after = (self.record.path / "flash-after.bin").read_bytes()
        self.assertEqual((self.record.path / "flash-before.bin").read_bytes(), BEFORE)
        self.assertEqual(after[:len(IMAGE)], IMAGE)
        self.assertEqual(after[len(IMAGE):program.ERASE_SIZE], b"\xff" * (program.ERASE_SIZE - len(IMAGE)))
        self.assertEqual(after[program.ERASE_SIZE:], BEFORE[program.ERASE_SIZE:])
        self.assertTrue(set(self.port.commands) <= {0, 1, 2, 0x11, 0x31, 0x44})
        self.assertEqual(self.port.reply, b"")
        events = [json.loads(line)["event"] for line in (self.record.path / "events.jsonl").read_text().splitlines()]
        self.assertLess(events.index("snapshot"), events.index("erase-intent"))
        self.assertEqual(events[-1], "verified")

    def test_option_complement_mismatch_blocks_erase(self):
        self.port.corrupt_option_complement = True
        with self.assertRaisesRegex(ProbeError, "complement mismatch"):
            self.run_program()
        self.assertNotIn(0x44, self.port.commands)
        self.assertFalse((self.record.path / "flash-before.bin").exists())

    def test_cancel_after_backup_without_erase(self):
        self.assertFalse(self.run_program("no")["flashed"])
        self.assertNotIn(0x44, self.port.commands)
        self.assertNotIn(0x31, self.port.commands)
        self.assertTrue((self.record.path / "flash-before.bin").exists())

    def test_backup_mismatch_saved_but_no_erase(self):
        self.port.memory[-1] ^= 1
        with self.assertRaisesRegex(ProbeError, "earlier verified"):
            self.run_program()
        self.assertTrue((self.record.path / "flash-before.bin").exists())
        self.assertNotIn(0x44, self.port.commands)

    def test_snapshot_failure_blocks_confirmation(self):
        confirm = Mock(return_value=program.CONFIRMATION)
        with patch.object(self.record, "snapshot", side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            self.programmer.run(confirm)
        confirm.assert_not_called()
        self.assertNotIn(0x44, self.port.commands)

    def test_intent_log_failure_blocks_erase(self):
        event = self.record.event
        def fail(name, **kwargs):
            if name == "erase-intent":
                raise OSError("disk full")
            event(name, **kwargs)
        with patch.object(self.record, "event", side_effect=fail), self.assertRaises(OSError):
            self.run_program()
        self.assertNotIn(0x44, self.port.commands)

    def test_identity_and_geometry_fail_closed(self):
        for attribute, value in (("pid", 0x466), ("version", 0x32), ("capabilities", b"\x00\x01\x02"),
                                 ("size", 256), ("optr", program.EXPECTED_OPTR ^ (1 << 20)),
                                 ("optr", program.EXPECTED_OPTR ^ (1 << 21))):
            port = MemoryRom()
            setattr(port, attribute, value)
            p = program.Programmer(port, IMAGE, self.record)
            with self.subTest(attribute=attribute), self.assertRaises(ProbeError):
                p.run(Mock(return_value=program.CONFIRMATION))
            self.assertNotIn(0x44, port.commands)

    def test_failed_erase_verification_blocks_write(self):
        self.port.corrupt_erase = True
        with self.assertRaisesRegex(ProbeError, "not all 0xFF"):
            self.run_program()
        self.assertNotIn(0x31, self.port.commands)

    def test_corrupt_image_readback_fails(self):
        self.port.corrupt_write = True
        with self.assertRaisesRegex(ProbeError, "readback mismatch"):
            self.run_program()
        self.assertEqual(self.programmer.phase, "failed")

    def test_outside_page_corruption_fails(self):
        self.port.change_outside = True
        with self.assertRaisesRegex(ProbeError, "readback mismatch"):
            self.run_program()

    def test_single_use_after_cancel(self):
        self.run_program("no")
        count = len(self.port.packets)
        with self.assertRaisesRegex(ProbeError, "single-use"):
            self.run_program()
        self.assertEqual(len(self.port.packets), count)

    def test_erase_ack_timeout_blocks_writes_and_retry(self):
        self.port.fail_stage, self.port.failure = "erase-pages", b""
        with self.assertRaises(ProbeError):
            self.run_program()
        self.assertTrue(self.programmer.destructive_started)
        self.assertNotIn(0x31, self.port.commands)
        self.assertEqual(self.port.commands.count(0x44), 1)
        count = len(self.port.packets)
        with self.assertRaises(ProbeError):
            self.run_program()
        self.assertEqual(len(self.port.packets), count)

    def test_write_ack_failure_stops_after_first_block(self):
        self.port.fail_stage = "write-data"
        with self.assertRaises(ProbeError):
            self.run_program()
        self.assertEqual(self.port.commands.count(0x31), 1)
        self.assertTrue(self.programmer.destructive_started)
        self.assertFalse((self.record.path / "flash-after.bin").exists())

    def test_readback_snapshot_failure_never_reports_success(self):
        original = self.record.snapshot
        def fail(name, data):
            if name == "flash-after.bin":
                raise OSError("disk full after programming")
            original(name, data)
        with patch.object(self.record, "snapshot", side_effect=fail), self.assertRaises(OSError):
            self.run_program()
        self.assertEqual(self.programmer.phase, "failed")
        self.assertTrue(self.programmer.destructive_started)

    def test_interruption_marks_failure_before_erase(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            self.programmer.run(Mock(side_effect=KeyboardInterrupt))
        self.assertFalse(self.programmer.destructive_started)
        self.assertEqual(self.programmer.phase, "failed")


class WireFailureTests(Fixture):
    def test_nack_noise_timeout_at_each_memory_stage(self):
        for stage in ("cmd-11", "read-address", "read-length", "cmd-44", "erase-pages",
                      "cmd-31", "write-address", "write-data"):
            for failure in (b"\x1f", b"\x00", b"\x76", b""):
                with self.subTest(stage=stage, failure=failure):
                    port = MemoryRom()
                    port.stage = "command"
                    port.fail_stage, port.failure = stage, failure
                    record = Mock()
                    p = program.Programmer(port, IMAGE, record)
                    with self.assertRaises(ProbeError):
                        if stage in ("cmd-44", "erase-pages"):
                            p.phase = "erase-authorized"
                            p._erase()
                        elif stage in ("cmd-31", "write-address", "write-data"):
                            p.phase = "erased-verified"
                            p._write_block(0, IMAGE[:8])
                        else:
                            p.read_memory(program.FLASH_BASE, 8)
                    self.assertEqual(len(port.commands), 1)

    def test_fragmented_reads(self):
        self.port.fragment = 1
        self.port.stage = "command"
        self.assertEqual(self.programmer.read_memory(program.FLASH_BASE, 256), IMAGE[:256])

    def test_truncated_read_payload(self):
        self.port.stage = "command"
        original = self.port.write
        def truncate(data):
            length = original(data)
            if len(data) == 2 and data == b"\x07\xf8":
                del self.port.reply[-1:]
            return length
        self.port.write = truncate
        with self.assertRaises(ProbeError):
            self.programmer.read_memory(program.FLASH_BASE, 8)

    def test_short_write_stops(self):
        self.port.write = Mock(return_value=0)
        with self.assertRaisesRegex(ProbeError, "short serial write"):
            self.programmer.read_memory(program.FLASH_BASE, 8)
        self.port.write.assert_called_once()

    def test_read_command_absolute_deadline(self):
        port = MemoryRom()
        port.stage = "command"
        p = program.Programmer(port, IMAGE, self.record, clock=Mock(side_effect=[0, 0.1, 0.2, 2.1]))
        with self.assertRaisesRegex(ProbeError, "deadline"):
            p.read_memory(program.FLASH_BASE, 8)

    def test_whole_read_deadline(self):
        p = program.Programmer(self.port, IMAGE, self.record, clock=Mock(side_effect=[0, 301]))
        with self.assertRaisesRegex(ProbeError, "whole flash read"):
            p._read_flash(program.FLASH_SIZE)
        self.assertEqual(self.port.packets, [])


class RecordTests(Fixture):
    def test_private_permissions_unique_runs_and_no_overwrite(self):
        self.record.snapshot("flash-before.bin", BEFORE)
        self.assertEqual(self.record.path.stat().st_mode & 0o777, 0o700)
        for path in self.record.path.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.record.snapshot("flash-before.bin", BEFORE)
        self.assertNotEqual(program.RunRecord(self.root / "runs").path, self.record.path)

    def test_invalid_snapshot(self):
        for name, data in (("../overwrite", BEFORE), ("flash-before.bin", b"short")):
            with self.assertRaises(ProbeError):
                self.record.snapshot(name, data)

    def test_fsync_failure_prevents_backup_completion(self):
        with patch.object(program.os, "fsync", side_effect=OSError("fsync failed")), self.assertRaises(OSError):
            self.record.snapshot("flash-before.bin", BEFORE)


class CliTests(Fixture):
    def invoke(self, extra=(), answers=(), tty=True):
        image = self.root / "image.bin"
        image.write_bytes(IMAGE)
        output = io.StringIO()
        mock_port = Mock()
        mock_port.__enter__ = Mock(return_value=self.port)
        mock_port.__exit__ = Mock(return_value=False)
        self.port.reset_input_buffer = Mock(side_effect=self.port.reply.clear)
        serial = Mock(SerialException=OSError)
        with patch.dict("sys.modules", {"serial": serial}), \
                patch.object(program.sys.stdin, "isatty", return_value=tty), \
                patch("builtins.input", side_effect=answers), \
                patch.object(program, "open_port", return_value=mock_port) as opened, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = program.main(["--image", str(image), "--backup-root", str(self.root / "cli-runs"),
                                   "--port", "/dev/ttyAMA0"] + list(extra))
        return status, opened, mock_port, output.getvalue()

    def test_default_offline_does_not_open_or_create_backup(self):
        status, opened, _, output = self.invoke()
        self.assertEqual(status, 0)
        opened.assert_not_called()
        self.assertFalse((self.root / "cli-runs").exists())
        self.assertIn("Offline", output)

    def test_diagnose_cli_needs_only_ready_and_reset(self):
        status, _, port, output = self.invoke(["--diagnose"], ["READY", "RESET"])
        self.assertEqual(status, 0)
        self.assertIn('"status": "geometry_verified"', output)
        self.assertNotIn(0x44, self.port.commands)
        self.assertNotIn(0x31, self.port.commands)
        port.__exit__.assert_called_once()

    def test_heartbeat_queued_while_waiting_for_reset_is_discarded(self):
        answers = iter(("READY", "RESET"))

        def answer(prompt):
            value = next(answers)
            if value == "RESET":
                # Application transmits after initial flush but before manual reset.
                self.port.reply.extend(b"Hello from NUCLEO-G0B1RE! firmware=0.2.0\r\n")
            return value

        status, _, port, output = self.invoke(["--diagnose"], answer)
        self.assertEqual(status, 0, output)
        self.assertEqual(self.port.reset_input_buffer.call_count, 2)
        self.assertEqual(self.port.packets[0], b"\x7f")
        self.assertNotIn(0x44, self.port.commands)
        port.__exit__.assert_called_once()

    def test_unexpected_reply_after_sync_still_stops_without_retry(self):
        self.port.fail_stage = "sync"
        self.port.failure = b"Hello from NUCLEO-G0B1RE! firmware=0.2.0\r\n"
        status, _, port, output = self.invoke(["--program"], ["READY", "RESET"])
        self.assertEqual(status, 1)
        self.assertIn("received 0x48", output)
        self.assertEqual(self.port.packets, [b"\x7f"])
        self.assertEqual(self.port.commands, [])
        port.__exit__.assert_called_once()

    def test_no_tty_and_wrong_port_refused(self):
        for extra, tty in ((["--program"], False), (["--program", "--port", "/dev/ttyS0"], True)):
            status, opened, _, _ = self.invoke(extra, tty=tty)
            self.assertEqual(status, 1)
            opened.assert_not_called()

    def test_invalid_image_refused_before_serial_open(self):
        with patch.object(program, "load_image", side_effect=ProbeError("bad image")):
            status, opened, _, _ = self.invoke(["--program"])
        self.assertEqual(status, 1)
        opened.assert_not_called()
        self.assertFalse((self.root / "cli-runs").exists())

    def test_open_failure_is_handled(self):
        image = self.root / "valid.bin"
        image.write_bytes(IMAGE)
        with patch.dict("sys.modules", {"serial": Mock(SerialException=OSError)}), \
                patch.object(program.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="READY"), \
                patch.object(program, "open_port", side_effect=OSError("busy")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(program.main(["--program", "--image", str(image), "--port", "/dev/ttyAMA0",
                                           "--backup-root", str(self.root / "cli-runs")]), 1)
        self.assertEqual(self.port.packets, [])

    def test_cancel_ready_no_open(self):
        status, opened, _, _ = self.invoke(["--program"], ["no"])
        self.assertEqual(status, 2)
        opened.assert_not_called()

    def test_cancel_reset_closes_no_sync(self):
        status, _, port, _ = self.invoke(["--program"], ["READY", "no"])
        self.assertEqual(status, 2)
        port.__exit__.assert_called_once()
        self.assertEqual(self.port.packets, [])

    def test_success_closes_and_reports(self):
        status, opened, port, output = self.invoke(["--program"], ["READY", "RESET", program.CONFIRMATION])
        self.assertEqual(status, 0)
        self.assertIn('"status": "flash_verified"', output)
        opened.assert_called_once()
        port.__exit__.assert_called_once()

    def test_cancel_final_confirmation(self):
        status, _, port, _ = self.invoke(["--program"], ["READY", "RESET", "no"])
        self.assertEqual(status, 2)
        port.__exit__.assert_called_once()
        self.assertNotIn(0x44, self.port.commands)

    def test_failure_after_erase_warns_and_closes(self):
        self.port.corrupt_write = True
        status, _, port, output = self.invoke(["--program"], ["READY", "RESET", program.CONFIRMATION])
        self.assertEqual(status, 1)
        self.assertIn("FLASH MAY BE INCOMPLETE", output)
        self.assertNotIn('"status": "flash_verified"', output)
        port.__exit__.assert_called_once()

    def test_interrupt_closes(self):
        status, _, port, _ = self.invoke(["--program"], ["READY", KeyboardInterrupt()])
        self.assertEqual(status, 130)
        port.__exit__.assert_called_once()


class PtyTests(Fixture):
    def test_real_pyserial_full_workflow_against_emulator(self):
        import serial
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        stop = threading.Event()
        errors = []

        def exact(count):
            data = bytearray()
            while len(data) < count:
                if stop.is_set():
                    return None
                if not select.select([master], [], [], 2)[0]:
                    if stop.is_set():
                        return None
                    raise AssertionError("PTY emulator timeout")
                chunk = os.read(master, count - len(data))
                if not chunk:
                    raise AssertionError("PTY closed")
                data.extend(chunk)
            return bytes(data)

        def emulate():
            try:
                while not stop.is_set():
                    length = {"sync": 1, "command": 2, "read-address": 5,
                              "write-address": 5, "read-length": 2, "erase-pages": 23,
                              "write-data": 1}[self.port.stage]
                    frame = exact(length)
                    if frame is None:
                        return
                    if self.port.stage == "write-data":
                        tail = exact(frame[0] + 2)
                        if tail is None:
                            return
                        frame += tail
                    self.port.write(frame)
                    reply = bytes(self.port.reply)
                    self.port.reply.clear()
                    while reply:
                        written = os.write(master, reply)
                        reply = reply[written:]
            except BaseException as exc:
                errors.append(exc)

        with program.open_port(serial, os.ttyname(slave), 115200, 2) as port:
            # PTYs do not support physical even parity; production remains 8E1.
            port.parity = serial.PARITY_NONE
            worker = threading.Thread(target=emulate, daemon=True)
            worker.start()
            try:
                self.programmer = program.Programmer(port, IMAGE, self.record)
                self.assertEqual(self.run_program()["status"], "flash_verified")
            finally:
                stop.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        self.assertFalse(port.is_open)


if __name__ == "__main__":
    unittest.main()