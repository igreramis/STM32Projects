"""Fake serial/clock only. No physical port, real-time waits, or serial writes."""

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import heartbeat_monitor as heartbeat


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakePort:
    def __init__(self, clock=None, data=b""):
        self.clock = clock or Clock()
        self.data = bytearray(data)
        self.timeout = 0.25
        self.opened = False
        self.closed = False
        self.dtr = True
        self.rts = True
        self.port = None
        self.read_error = None
        self.open_error = None
        self.cleared = False
        self.write = Mock(side_effect=AssertionError("receive-only monitor attempted TX"))

    def open(self):
        assert self.dtr is False and self.rts is False
        assert self.port == "/dev/ttyAMA0"
        if self.open_error:
            raise self.open_error
        self.opened = True

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        # Fixture data represents future arrivals, not a stale OS input queue.
        self.cleared = True

    def read(self, count):
        assert count == 1
        if self.read_error:
            raise self.read_error
        if self.data:
            result = bytes(self.data[:count])
            del self.data[:count]
            self.clock.now += 0.001
            return result
        self.clock.now += self.timeout
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeSerialError(Exception):
    pass


def serial_module(port):
    return SimpleNamespace(Serial=Mock(return_value=port), EIGHTBITS=8, PARITY_NONE="N",
                           STOPBITS_ONE=1, SerialException=FakeSerialError)


def line(version="0.2.0"):
    return heartbeat.PREFIX + version.encode("ascii") + b"\r\n"


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = FakePort(self.clock)
        self.module = serial_module(self.port)
        self.output = io.StringIO()
        for redirect in (contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output)):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)
        self.addCleanup(self.port.write.assert_not_called)

    def monitor(self, **kwargs):
        return heartbeat.monitor(self.port, clock=self.clock, **kwargs)

    def run_main(self, *args):
        real_monitor = heartbeat.monitor

        def timed_monitor(port, version, count, timeout):
            return real_monitor(port, version, count, timeout, clock=self.clock)

        with patch.dict("sys.modules", {"serial": self.module}), \
                patch.object(heartbeat, "monitor", side_effect=timed_monitor):
            return heartbeat.main(["--port", "/dev/ttyAMA0", *args])

    def test_closed_first_configuration_before_open(self):
        self.assertIs(heartbeat.open_port(self.module, "/dev/ttyAMA0"), self.port)
        self.module.Serial.assert_called_once_with(
            port=None, baudrate=115200, bytesize=8, parity="N", stopbits=1,
            timeout=0.25, write_timeout=0.25, xonxoff=False,
            rtscts=False, dsrdtr=False, exclusive=True)
        self.assertTrue(self.port.opened)
        self.assertFalse(self.port.dtr)
        self.assertFalse(self.port.rts)
        self.port.close()

    def test_failed_open_closes_port(self):
        for error in (FakeSerialError("busy"), OSError("failed"), KeyboardInterrupt()):
            self.port.closed = False
            self.port.open_error = error
            with self.assertRaises(type(error)):
                heartbeat.open_port(self.module, "/dev/ttyAMA0")
            self.assertTrue(self.port.closed)

    def test_count_matching_lines_and_stop_without_extra_reads(self):
        self.port.data.extend(line() * 2 + b"not consumed")
        self.assertEqual(self.monitor(count=2), 2)
        self.assertEqual(self.port.data, b"not consumed")
        self.assertEqual(self.output.getvalue().count(" MATCH "), 2)

    def test_timestamp_output_is_flushed(self):
        self.port.data.extend(line())
        with patch("builtins.print") as output:
            self.assertEqual(self.monitor(count=1), 1)
        output.assert_called_once()
        text = output.call_args[0][0]
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T.*\+00:00 MATCH ")
        self.assertTrue(output.call_args[1]["flush"])

    def test_exact_versions_only_and_optional_version_filter(self):
        self.port.data.extend(b"noise\n" + line("0.1.0") + line("0.3.0") + line())
        self.assertEqual(self.monitor(version="0.2.0", count=1), 1)
        self.assertEqual(self.output.getvalue().count(" MISMATCH "), 3)
        self.assertEqual(self.output.getvalue().count(" MATCH "), 1)

    def test_all_known_versions_match_without_filter(self):
        self.port.data.extend(b"".join(line(version) for version in heartbeat.VERSIONS))
        self.assertEqual(self.monitor(count=3), 3)

    def test_malformed_unknown_original_and_partial_lines_do_not_count(self):
        for data in (line("0.4.0"), b"Hello from NUCLEO-G0B1RE!\r\n", line()[:-1],
                     b" " + line(), line().replace(b"\r\n", b" \r\n")):
            self.port.data = bytearray(data)
            with self.assertRaises(heartbeat.MonitorError):
                self.monitor(count=1, timeout=1)

    def test_lf_and_crlf_supported(self):
        self.port.data.extend(line().replace(b"\r\n", b"\n") + line())
        self.assertEqual(self.monitor(count=2), 2)

    def test_terminal_controls_and_invalid_bytes_escaped(self):
        self.port.data.extend(b"\x1b[31m\x00\xff\rX\n" + line())
        self.assertEqual(self.monitor(count=1), 1)
        output = self.output.getvalue()
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x00", output)
        self.assertNotIn("\r", output)
        self.assertIn("\\u001b", output)
        self.assertIn("\\xff", output)

    def test_count_deadline_is_bounded_even_with_no_bytes(self):
        with self.assertRaisesRegex(heartbeat.MonitorError, "0/2 matches"):
            self.monitor(count=2, timeout=0.6)
        self.assertAlmostEqual(self.clock.now, 0.6)

    def test_count_default_deadline(self):
        with self.assertRaises(heartbeat.MonitorError):
            self.monitor(count=1)
        self.assertEqual(self.clock.now, heartbeat.DEFAULT_COUNT_TIMEOUT)

    def test_timeout_only_is_bounded_observation_not_count_assertion(self):
        self.port.data.extend(line())
        self.assertEqual(self.monitor(timeout=0.6), 1)
        self.assertAlmostEqual(self.clock.now, 0.6)

    def test_deadline_is_not_reset_by_arriving_data(self):
        self.port.data.extend(line() * 100)
        with self.assertRaises(heartbeat.MonitorError):
            self.monitor(count=100, timeout=0.2)
        self.assertLess(self.clock.now, 0.202)

    def test_late_line_cannot_satisfy_count(self):
        self.port.data.extend(line())
        original_read = self.port.read

        def delayed_read(count):
            chunk = original_read(count)
            if chunk == b"\n":
                self.clock.now += 5
            return chunk

        self.port.read = delayed_read
        with self.assertRaises(heartbeat.MonitorError):
            self.monitor(count=1, timeout=1)
        self.assertNotIn(" MATCH ", self.output.getvalue())

    def test_continuous_has_no_implicit_deadline_or_count(self):
        calls = 0

        def read(count):
            nonlocal calls
            calls += 1
            self.clock.now += 60
            if calls == 4:
                raise KeyboardInterrupt
            return b"\n"

        self.port.read = read
        with self.assertRaises(KeyboardInterrupt):
            self.monitor()
        self.assertEqual(calls, 4)
        self.assertEqual(self.output.getvalue().count(" MISMATCH "), 3)

    def test_line_buffer_is_bounded(self):
        self.port.data.extend(b"x" * (heartbeat.MAX_LINE_BYTES + 1))
        with self.assertRaisesRegex(heartbeat.MonitorError, "exceeds 512"):
            self.monitor(count=1, timeout=5)

    def test_cli_count_success_closes_and_clears_input(self):
        self.port.data.extend(line() * 2)
        self.assertEqual(self.run_main("--count", "2", "--timeout", "1"), 0)
        self.assertTrue(self.port.opened)
        self.assertTrue(self.port.closed)
        self.assertTrue(self.port.cleared)

    def test_cli_count_timeout_failure_closes(self):
        self.assertEqual(self.run_main("--count", "2", "--timeout", "0.1"), 1)
        self.assertTrue(self.port.closed)

    def test_cli_timeout_only_success_closes(self):
        self.assertEqual(self.run_main("--timeout", "0.1"), 0)
        self.assertTrue(self.port.closed)

    def test_cli_interrupt_and_read_failure_close(self):
        for error, expected in ((KeyboardInterrupt(), 130), (FakeSerialError("read failed"), 1),
                                (OSError("device removed"), 1)):
            self.port.closed = False
            self.port.read_error = error
            self.assertEqual(self.run_main(), expected)
            self.assertTrue(self.port.closed)

    def test_cli_open_failure_closes(self):
        self.port.open_error = FakeSerialError("exclusive lock held")
        self.assertEqual(self.run_main(), 1)
        self.assertTrue(self.port.closed)

    def test_cli_line_overflow_closes(self):
        self.port.data.extend(b"x" * (heartbeat.MAX_LINE_BYTES + 1))
        self.assertEqual(self.run_main(), 1)
        self.assertTrue(self.port.closed)

    def test_cli_defaults_are_continuous_serial0(self):
        with patch.object(Path, "resolve", return_value=Path("/dev/ttyAMA0")), \
                patch.dict("sys.modules", {"serial": self.module}), \
                patch.object(heartbeat, "open_port", return_value=self.port) as opener, \
                patch.object(heartbeat, "monitor") as monitor:
            self.assertEqual(heartbeat.main([]), 0)
            opener.assert_called_once_with(self.module, "/dev/serial0")
            monitor.assert_called_once_with(self.port, None, None, None)
        self.assertTrue(self.port.closed)

    def test_non_pl011_rejected_before_open(self):
        with patch.dict("sys.modules", {"serial": self.module}):
            self.assertEqual(heartbeat.main(["--port", "/dev/ttyS0"]), 1)
        self.module.Serial.assert_not_called()

    def test_cli_invalid_options_rejected_without_open(self):
        for args in (("--count", "0"), ("--count", "-1"), ("--count", "1.5"),
                     ("--timeout", "0"), ("--timeout", "-1"), ("--timeout", "nan"),
                     ("--timeout", "inf"), ("--timeout", "bad"), ("--version", "original")):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                self.run_main(*args)
            self.assertEqual(error.exception.code, 2)
        self.module.Serial.assert_not_called()

    def test_direct_invalid_arguments(self):
        for kwargs in ({"version": "unknown"}, {"count": True}, {"count": 0}, {"count": 1.5},
                       {"timeout": float("nan")}, {"timeout": float("inf")}, {"timeout": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.monitor(**kwargs)


if __name__ == "__main__":
    unittest.main()