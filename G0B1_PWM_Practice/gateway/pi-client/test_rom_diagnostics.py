"""Offline identification diagnostics; no physical UART or network."""

import contextlib
import io
import unittest
from unittest.mock import Mock

import ota_installer as ota
from rom_read_id import ProbeError, ProtocolError
from test_rom_read_id import FakePort, REPLY, TX


class DiagnosticTests(unittest.TestCase):
    def probe(self, reply, configure=None):
        port = FakePort(reply, fragment=1)
        # Canned responses represent bytes arriving after each request.
        port.reset_input_buffer = Mock()
        probe = ota.WaitingRomProbe(port)
        record = Mock()
        probe.record = record
        if configure:
            configure(port)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises((ProbeError, OSError)):
                probe.run()
        return port, record, output.getvalue()

    def test_get_failures_have_precise_local_stage_and_reason(self):
        cases = (
            (b"\x79", "command 0x00", "timeout"),
            (b"\x79\x1f", "command 0x00", "nack"),
            (b"\x79y", "Get length", "timeout"),
            (b"\x79t", "command 0x00", "unexpected_byte"),
            (b"\x79\x79\x04\x31", "Get payload", "timeout"),
            (REPLY[:8], "Get trailing ACK", "timeout"),
            (REPLY[:8] + b"\x1f", "Get trailing ACK", "nack"),
            (REPLY[:8] + b"x", "Get trailing ACK", "unexpected_byte"),
            (bytes.fromhex("79 79 01 31 00 79"), "Get validation", "missing_commands"),
        )
        for reply, stage, reason in cases:
            with self.subTest(stage=stage, reason=reason):
                port, record, output = self.probe(reply)
                record.event.assert_called_once_with(
                    "rom-probe-failed", stage=stage, reason=reason)
                self.assertIn("stage=" + stage + " reason=" + reason, output)
                self.assertEqual(port.writes, TX[:2])
                port.reset_input_buffer.assert_called_once_with()

    def test_transport_exception_is_not_printed_or_persisted(self):
        secret = "https://secret.invalid/?token=PRIVATE"

        def configure(port):
            original = port.write

            def write(data):
                if data == TX[1]:
                    raise OSError(secret)
                return original(data)

            port.write = write

        _, record, output = self.probe(REPLY, configure)
        record.event.assert_called_once_with(
            "rom-probe-failed", stage="get send", reason="transport_error")
        self.assertNotIn(secret, output + str(record.mock_calls))

    def test_short_get_write_has_distinct_reason(self):
        def configure(port):
            original = port.write
            port.write = lambda data: 0 if data == TX[1] else original(data)

        _, record, _ = self.probe(REPLY, configure)
        record.event.assert_called_once_with(
            "rom-probe-failed", stage="get send", reason="short_write")

    def test_read_transport_failure_reports_current_stage(self):
        def configure(port):
            original = port.read

            def read(count):
                if len(port.writes) == 2:
                    raise OSError("PRIVATE")
                return original(count)

            port.read = read

        _, record, output = self.probe(REPLY, configure)
        record.event.assert_called_once_with(
            "rom-probe-failed", stage="command 0x00", reason="transport_error")
        self.assertNotIn("PRIVATE", output + str(record.mock_calls))

    def test_untrusted_error_metadata_cannot_leak(self):
        def configure(port):
            original = port.write

            def write(data):
                if data == TX[1]:
                    raise ProtocolError("PRIVATE", "PRIVATE", "PRIVATE")
                return original(data)

            port.write = write

        _, record, output = self.probe(REPLY, configure)
        record.event.assert_called_once_with(
            "rom-probe-failed", stage="unknown", reason="protocol_error")
        self.assertNotIn("PRIVATE", output + str(record.mock_calls))

    def test_deadline_expiry_is_distinct_from_empty_read(self):
        port = FakePort(REPLY)
        port.reset_input_buffer = Mock()
        now = [0.0]
        probe = ota.WaitingRomProbe(port, clock=lambda: now[0])
        probe.record = Mock()
        original = port.read

        def read(count):
            reply = original(count)
            if len(port.writes) == 2:
                # Command ACK completed at its deadline; no time for length.
                now[0] = 2.0
            return reply

        port.read = read
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ProbeError):
                probe.run()
        probe.record.event.assert_called_once_with(
            "rom-probe-failed", stage="Get length", reason="deadline_expired")
        self.assertEqual(port.writes, TX[:2])

    def test_record_failure_preserves_protocol_error_and_stops(self):
        port = FakePort(b"\x79\x1f")
        port.reset_input_buffer = Mock()
        probe = ota.WaitingRomProbe(port)
        probe.record = Mock()
        probe.record.event.side_effect = OSError("PRIVATE")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(ProbeError, "NACK"):
                probe.run()
        self.assertIn("diagnostic persistence failed", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(port.writes, TX[:2])

    def test_success_wire_transcript_unchanged(self):
        port = FakePort(REPLY, fragment=1)
        port.reset_input_buffer = Mock()
        probe = ota.WaitingRomProbe(port)
        probe.record = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            result = probe.run()
        self.assertEqual(result["status"], "rom_identified")
        self.assertEqual(port.writes, TX)
        probe.record.event.assert_not_called()


if __name__ == "__main__":
    unittest.main()