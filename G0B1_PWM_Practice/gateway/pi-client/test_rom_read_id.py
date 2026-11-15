import contextlib
import io
import os
import pty
import select
import threading
import unittest
from unittest.mock import Mock, patch

import rom_read_id as rom


# Synthetic ROM V9.2 transcript. Not captured from the physical board.
REPLY = bytes.fromhex("79 79 04 92 00 01 02 31 79 79 92 00 00 79 79 01 04 67 79")
TX = [b"\x7f", b"\x00\xff", b"\x01\xfe", b"\x02\xfd"]


class FakePort:
    def __init__(self, reply=REPLY, fragment=256):
        self.reply = bytearray(reply)
        self.fragment = fragment
        self.writes = []
        self.timeout = 0
        self.write_timeout = 0

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read(self, count):
        count = min(count, self.fragment)
        value = bytes(self.reply[:count])
        del self.reply[:count]
        return value


class ProtocolTests(unittest.TestCase):
    def test_success_exact_read_only_transcript(self):
        port = FakePort()
        result = rom.RomProbe(port).run()
        self.assertEqual(port.writes, TX)
        self.assertEqual(port.reply, b"")
        self.assertEqual(result["device_id"], "0x0467")
        self.assertEqual(result["bootloader_version"], "9.2")
        self.assertIn("0x31", result["supported_commands"])
        self.assertFalse(result["flashed"])
        self.assertFalse(result["application_health_verified"])

    def test_fragmented_reads(self):
        self.assertEqual(rom.RomProbe(FakePort(fragment=1)).run()["status"], "rom_identified")

    def test_every_truncated_response_fails_without_retry(self):
        for length in range(len(REPLY)):
            with self.subTest(length=length):
                port = FakePort(REPLY[:length])
                with self.assertRaises(rom.ProbeError):
                    rom.RomProbe(port).run()
                self.assertEqual(port.writes, TX[:len(port.writes)])

    def test_all_ack_positions_reject_nack_and_noise(self):
        for position in (0, 1, 8, 9, 13, 14, 18):
            for value in (0x1F, 0x00, 0x76):
                with self.subTest(position=position, value=value):
                    reply = bytearray(REPLY)
                    reply[position] = value
                    with self.assertRaises(rom.ProbeError):
                        rom.RomProbe(FakePort(reply)).run()

    def test_missing_required_command_stops_before_version(self):
        reply = bytearray(REPLY)
        reply[6] = 0x11
        port = FakePort(reply)
        with self.assertRaisesRegex(rom.ProbeError, "advertise"):
            rom.RomProbe(port).run()
        self.assertEqual(port.writes, TX[:2])

    def test_version_mismatch(self):
        reply = bytearray(REPLY)
        reply[10] = 0x91
        with self.assertRaisesRegex(rom.ProbeError, "disagree"):
            rom.RomProbe(FakePort(reply)).run()

    def test_wrong_device(self):
        reply = bytearray(REPLY)
        reply[17] = 0x60
        with self.assertRaisesRegex(rom.ProbeError, "wrong device"):
            rom.RomProbe(FakePort(reply)).run()

    def test_id_length_rejected(self):
        for length in (0, 2, 255):
            reply = bytearray(REPLY)
            reply[15] = length
            with self.subTest(length=length), self.assertRaisesRegex(rom.ProbeError, "two-byte"):
                rom.RomProbe(FakePort(reply)).run()

    def test_oversized_get_is_bounded_by_length_byte(self):
        port = FakePort(bytes.fromhex("79 79 ff"))
        with self.assertRaises(rom.ProbeError):
            rom.RomProbe(port).run()
        self.assertEqual(port.writes, TX[:2])

    def test_short_write(self):
        port = FakePort()
        port.write = Mock(return_value=0)
        with self.assertRaisesRegex(rom.ProbeError, "short serial write"):
            rom.RomProbe(port).run()
        port.write.assert_called_once_with(b"\x7f")

    def test_absolute_deadline_not_reset_by_partial_reads(self):
        clock = Mock(side_effect=(0.0, 0.1, 1.1))
        port = FakePort(b"ab", fragment=1)
        with self.assertRaisesRegex(rom.ProbeError, "deadline"):
            rom.RomProbe(port, clock=clock)._read(2, 1.0, "test")

    def test_late_read_is_not_accepted(self):
        clock = Mock(side_effect=(0.0, 1.1))
        with self.assertRaises(rom.ProbeError):
            rom.RomProbe(FakePort(b"a"), clock=clock)._read(1, 1.0, "test")

    def test_all_other_commands_rejected_without_transmission(self):
        port = FakePort()
        probe = rom.RomProbe(port)
        for command in set(range(256)) - rom.READ_ONLY_COMMANDS:
            with self.assertRaises(rom.ProbeError):
                probe._command(command)
        self.assertEqual(port.writes, [])

    def test_invalid_timeout(self):
        for timeout in (0, -1, 11, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                rom.RomProbe(FakePort(), timeout)


class CliTests(unittest.TestCase):
    def run_cli(self, answers, error=None):
        serial = Mock(SerialException=OSError)
        port = serial.Serial.return_value
        port.__enter__ = Mock(return_value=port)
        port.__exit__ = Mock(return_value=False)
        port.write.side_effect = FakePort().write
        port.read.side_effect = FakePort().read
        if error is not None:
            port.read.side_effect = error
        output = io.StringIO()
        with patch.dict("sys.modules", {"serial": serial}), \
                patch.object(rom.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=answers), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = rom.main(["--port", "/dev/ttyAMA0"])
        return status, serial, port, output.getvalue()

    def test_cli_success_and_cleanup(self):
        status, serial, port, output = self.run_cli(["READY", "RESET"])
        self.assertEqual(status, 0)
        self.assertIn('"flashed": false', output)
        port.__exit__.assert_called_once()
        self.assertEqual([c.args[0] for c in port.write.call_args_list], TX)
        port.reset_input_buffer.assert_called_once_with()

    def test_cancel_before_open(self):
        status, serial, port, output = self.run_cli(["no"])
        self.assertEqual(status, 2)
        serial.Serial.assert_not_called()

    def test_cancel_before_sync_closes_without_bytes(self):
        status, serial, port, output = self.run_cli(["READY", "no"])
        self.assertEqual(status, 2)
        port.write.assert_not_called()
        port.__exit__.assert_called_once()

    def test_serial_failure_closes(self):
        status, serial, port, output = self.run_cli(["READY", "RESET"], OSError("disconnected"))
        self.assertEqual(status, 1)
        self.assertNotIn('"status": "rom_identified"', output)
        port.__exit__.assert_called_once()

    def test_interrupt_closes(self):
        status, serial, port, output = self.run_cli(["READY", KeyboardInterrupt()])
        self.assertEqual(status, 130)
        port.write.assert_not_called()
        port.__exit__.assert_called_once()

    def test_serial_configuration(self):
        serial = Mock(EIGHTBITS=8, PARITY_EVEN="E", STOPBITS_ONE=1)
        port = rom.open_port(serial, "/dev/ttyAMA0", 115200, 2)
        serial.Serial.assert_called_once_with(
            port=None, baudrate=115200, bytesize=8, parity="E", stopbits=1,
            timeout=2, write_timeout=2, xonxoff=False, rtscts=False,
            dsrdtr=False, exclusive=True)
        self.assertFalse(port.dtr)
        self.assertFalse(port.rts)
        port.open.assert_called_once_with()

    def test_open_failure_closes_port(self):
        serial = Mock()
        serial.Serial.return_value.open.side_effect = OSError("busy")
        with self.assertRaises(OSError):
            rom.open_port(serial, "/dev/ttyAMA0", 115200, 2)
        serial.Serial.return_value.close.assert_called_once_with()

    def test_noninteractive_refused(self):
        with patch.object(rom.sys.stdin, "isatty", return_value=False), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rom.main([]), 2)

    def test_mini_uart_refused(self):
        with patch.object(rom.sys.stdin, "isatty", return_value=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rom.main(["--port", "/dev/ttyS0"]), 2)


class PseudoTerminalTests(unittest.TestCase):
    """Real pySerial + Linux PTY, not physical UART/parity/STM32 validation."""

    def setUp(self):
        import serial
        self.serial = serial
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.device = os.ttyname(self.slave)

    def test_real_serial_transport_with_rom_emulator(self):
        errors = []
        replies = (REPLY[:1], REPLY[1:9], REPLY[9:14], REPLY[14:])

        def emulator():
            try:
                for expected, reply in zip(TX, replies):
                    received = bytearray()
                    while len(received) < len(expected):
                        if not select.select([self.master], [], [], 3)[0]:
                            raise AssertionError("emulator timed out waiting for command")
                        received.extend(os.read(self.master, len(expected) - len(received)))
                    self.assertEqual(received, expected)
                    os.write(self.master, reply)
            except BaseException as exc:
                errors.append(exc)

        with rom.open_port(self.serial, self.device, 115200, 1) as port:
            # Linux PTYs do not implement physical parity. Use 8N1 ONLY for
            # this simulated transport; open_port's 8E1 is tested separately.
            port.parity = self.serial.PARITY_NONE
            worker = threading.Thread(target=emulator, daemon=True)
            worker.start()
            try:
                self.assertEqual(rom.RomProbe(port, 1).run()["device_id"], "0x0467")
            finally:
                worker.join(4)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        self.assertFalse(port.is_open)

    def test_exclusive_lock_rejects_second_cooperating_open(self):
        with rom.open_port(self.serial, self.device, 115200, 1):
            with self.assertRaises(self.serial.SerialException):
                rom.open_port(self.serial, self.device, 115200, 1)

    def test_real_serial_timeout(self):
        with rom.open_port(self.serial, self.device, 115200, 0.1) as port:
            port.parity = self.serial.PARITY_NONE
            with self.assertRaisesRegex(rom.ProbeError, "timed out"):
                rom.RomProbe(port, 0.1).run()


if __name__ == "__main__":
    unittest.main()