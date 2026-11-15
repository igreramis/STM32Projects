"""Parser, freshness and UART handoff tests; no serial hardware or MQTT."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import client
import uart_state
from test_heartbeat_monitor import Clock, line


def sample(**changes):
    value = dict(type="state", device_id="stm32g0b1-01", firmware="0.4.0", state="idle", sequence=0)
    value.update(changes)
    return json.dumps(value).encode()


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.wall = Clock()
        self.wall.now = 1700000000
        self.monitor = uart_state.StateMonitor("/dev/ttyAMA0", self.clock, self.wall)

    def test_exact_contract_empty_and_valid_snapshot(self):
        expected = {"device_id", "firmware", "state", "sequence", "last_seen", "age_seconds", "updating"}
        empty = self.monitor.snapshot()
        self.assertEqual(set(empty), expected)
        self.assertIsNone(empty["last_seen"])
        self.monitor.feed(sample() + b"\r\n")
        value = self.monitor.snapshot()
        self.assertEqual(set(value), expected)
        self.assertEqual(value["firmware"], "0.4.0")
        self.assertEqual(value["state"], "idle")
        self.assertEqual(value["sequence"], 0)
        self.assertEqual(value["age_seconds"], 0)

    def test_fragmented_stream_and_freshness_not_publications(self):
        for byte in sample():
            self.monitor.feed(bytes([byte]))
        self.assertIsNone(self.monitor.snapshot()["last_seen"])
        self.monitor.feed(b"\n")
        first = self.monitor.snapshot()
        self.clock.now = 36
        self.wall.now -= 10000  # NTP correction cannot make old UART data fresh.
        for _ in range(10):
            later = self.monitor.snapshot()
            self.assertEqual(later["last_seen"], first["last_seen"])
            self.assertEqual(later["age_seconds"], 36)
        self.monitor.feed(b"invalid\n")
        self.assertEqual(self.monitor.snapshot(), later)

    def test_all_states_uint32_wrap_and_legacy_heartbeat(self):
        for index, state in enumerate(uart_state.STATES):
            self.monitor.feed(sample(state=state, sequence=0xffffffff - index) + b"\n")
            self.assertEqual(self.monitor.snapshot()["state"], state)
        self.monitor.feed(sample(sequence=0) + b"\n")
        self.assertEqual(self.monitor.snapshot()["sequence"], 0)
        self.monitor.feed(line("0.2.0"))
        value = self.monitor.snapshot()
        self.assertEqual(value["firmware"], "0.2.0")
        self.assertIsNone(value["state"])
        self.assertIsNone(value["sequence"])
        self.monitor.feed(line("123.456.789"))
        self.assertEqual(self.monitor.snapshot()["firmware"], "123.456.789")

    def test_same_sequence_after_reboot_is_a_fresh_uart_observation(self):
        self.monitor.feed(sample() + b"\n")
        seen = self.monitor.snapshot()["last_seen"]
        self.clock.now = 40
        self.wall.now += 40
        self.monitor.feed(sample() + b"\n")
        self.assertNotEqual(self.monitor.snapshot()["last_seen"], seen)
        self.assertEqual(self.monitor.snapshot()["age_seconds"], 0)
        self.monitor.feed(sample(sequence=1) + b"\n")
        self.assertEqual(self.monitor.snapshot()["age_seconds"], 0)

    def test_heartbeat_preserves_recent_state_but_cannot_extend_its_freshness(self):
        self.monitor.feed(sample(state="running", sequence=42) + b"\n")
        self.clock.now = 10
        self.wall.now += 10
        self.monitor.feed(line("0.4.0"))
        current = self.monitor.snapshot()
        self.assertEqual((current["state"], current["sequence"]), ("running", 42))
        self.assertEqual(current["age_seconds"], 0)
        self.clock.now = 35
        self.wall.now += 25
        self.monitor.feed(line("0.4.0"))
        self.assertIsNone(self.monitor.snapshot()["state"])
        self.assertIsNone(self.monitor.snapshot()["sequence"])
        self.assertEqual(self.monitor.snapshot()["age_seconds"], 0)
        self.monitor.feed(sample(state="paused", sequence=43) + b"\n")
        self.assertEqual(self.monitor.snapshot()["state"], "paused")

    def test_firmware_change_clears_old_state(self):
        self.monitor.feed(sample() + b"\n")
        self.monitor.feed(line("0.5.0"))
        self.assertIsNone(self.monitor.snapshot()["state"])
        self.assertEqual(self.monitor.snapshot()["firmware"], "0.5.0")

    def test_gateway_pending_job_marks_updating_without_fabricating_observation(self):
        gateway = client.Gateway.__new__(client.Gateway)
        gateway.config = SimpleNamespace(client_id="pi-lab-01")
        gateway.boot_id = "boot"
        gateway.monitor = self.monitor
        gateway.ota = Mock()
        gateway.ota.status.return_value = {"active": True}
        with patch.object(client, "snapshot", return_value={}):
            value = gateway.telemetry(1)["subdevices"][0]
        self.assertTrue(value["updating"])
        self.assertIsNone(value["last_seen"])
        self.assertIsNone(value["firmware"])

    def test_reject_malformed_exact_schema_and_bounds(self):
        bad = [sample(type="other"), sample(device_id="other"), sample(firmware="01.2.3"),
               sample(state="unknown"), sample(state=[]), sample(sequence=True), sample(sequence=1.0),
               sample(sequence=-1), sample(sequence=2**32), sample(extra=True), b"[]", b"NaN", b"\xff",
               b"[" * 513, sample().replace(b'"sequence": 0', b'"sequence": 0, "sequence": 1'),
               b" " + line().strip(), line().strip() + b" "]
        for value in bad:
            with self.subTest(value=value[:50]):
                self.assertIsNone(uart_state.parse_line(value))
        value = json.loads(sample())
        for field in value:
            self.assertIsNone(uart_state.parse_line(json.dumps({k: v for k, v in value.items() if k != field}).encode()))

    def test_oversized_line_discards_valid_suffix_and_recovers(self):
        self.monitor.feed(b"x" * 100000 + sample() + b"\n")
        self.assertIsNone(self.monitor.snapshot()["last_seen"])
        self.assertLessEqual(len(self.monitor._pending), 512)
        self.monitor.feed(sample() + b"\n")
        self.assertEqual(self.monitor.snapshot()["state"], "idle")

    def test_gateway_boot_and_samples_without_mqtt(self):
        gateway = client.Gateway.__new__(client.Gateway)
        gateway.config = SimpleNamespace(client_id="pi-lab-01")
        gateway.boot_id = "test-process-boot"
        gateway.ota = None
        gateway.monitor = self.monitor
        self.monitor.feed(sample() + b"\n")
        with patch.object(client, "snapshot", side_effect=lambda device, count: dict(device_id=device, telemetry_count=count)):
            first = gateway.telemetry(1)
            self.clock.now += 40
            second = gateway.telemetry(2)
        self.assertEqual(first["boot_id"], second["boot_id"])
        self.assertEqual(first["subdevices"][0]["last_seen"], second["subdevices"][0]["last_seen"])
        self.assertEqual(second["subdevices"][0]["age_seconds"], 40)

    def test_command_limit_and_duplicate_json_keys(self):
        self.assertEqual(client.MAX_COMMAND_BYTES, 8192)
        with self.assertRaises(ValueError):
            client.parse_command_object(b"x" * 8193)
        with self.assertRaises(ValueError):
            client.parse_command_object(b'{"command_id":"x","command":"get_status","command":"install_firmware"}')


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.reading = threading.Event()
        self.release_read = threading.Event()
        self.close_failed = False
        self.opens = 0
        self.monitor = uart_state.StateMonitor("/dev/ttyAMA0", opener=self.open)
        self.addCleanup(self.cleanup_monitor)
        self.addCleanup(self.release_read.set)

    def cleanup_monitor(self):
        try:
            self.monitor.close()
        except RuntimeError:
            # Some tests deliberately simulate an unconfirmable port close.
            self.assertFalse(self.monitor._released)

    def open(self, device):
        self.opens += 1
        self.events.append("open")
        return SimpleNamespace(read=self.read, close=self.close,
                               reset_input_buffer=lambda: self.events.append("clear"))

    def read(self, count):
        self.reading.set()
        self.release_read.wait(3)
        return b""

    def close(self):
        self.events.append("close")
        if self.close_failed:
            raise OSError("simulated close failure")

    def test_pause_joins_close_before_worker_and_resumes_after_failure(self):
        self.monitor.start()
        self.assertTrue(self.reading.wait(3))
        self.release_read.set()
        worker = Mock()
        worker.uart_released = True

        def install(*args):
            self.events.append("worker")
            self.assertTrue(self.monitor.snapshot()["updating"])
            self.assertEqual(self.events[-2], "close")
            raise RuntimeError("installer failed")

        worker.side_effect = install
        wrapper = uart_state.MonitoredInstaller(worker, self.monitor)
        with self.assertRaises(RuntimeError):
            wrapper({}, Mock(), threading.Event())
        self.monitor.close()
        self.assertEqual(self.opens, 2)
        self.assertFalse(self.monitor.snapshot()["updating"])

    def test_failed_monitor_close_never_calls_installer(self):
        self.close_failed = True
        self.monitor.start()
        self.assertTrue(self.reading.wait(3))
        self.release_read.set()
        worker = Mock()
        with self.assertRaisesRegex(RuntimeError, "uart_not_released"):
            uart_state.MonitoredInstaller(worker, self.monitor)({}, Mock(), threading.Event())
        worker.assert_not_called()
        self.assertEqual(self.opens, 1)
        self.assertFalse(self.monitor.snapshot()["updating"])

    def test_worker_close_uncertainty_inhibits_monitor_and_next_worker(self):
        self.monitor.start()
        self.assertTrue(self.reading.wait(3))
        self.release_read.set()
        worker = Mock(return_value={"status": "error"})
        worker.uart_released = False
        wrapper = uart_state.MonitoredInstaller(worker, self.monitor)
        wrapper({}, Mock(), threading.Event())
        self.assertEqual(self.opens, 1)
        with self.assertRaisesRegex(RuntimeError, "uart_not_released"):
            wrapper({}, Mock(), threading.Event())
        worker.assert_called_once()

    def test_partial_line_discarded_across_handoff(self):
        self.monitor.feed(sample()[:-1])
        self.monitor.pause()
        self.release_read.set()
        self.monitor.resume()
        self.monitor.close()
        self.monitor.feed(b"}\n")
        self.assertIsNone(self.monitor.snapshot()["last_seen"])

    def test_failed_open_never_hands_uart_to_worker(self):
        self.monitor.opener = Mock(side_effect=OSError("open uncertainty"))
        self.monitor.start()
        with self.assertRaisesRegex(RuntimeError, "uart_not_released"):
            self.monitor.pause()

    def test_monitor_close_uncertainty_is_reported_to_lifetime_owner(self):
        self.close_failed = True
        self.monitor.start()
        self.assertTrue(self.reading.wait(3))
        self.release_read.set()
        with self.assertRaisesRegex(RuntimeError, "uart_not_released"):
            self.monitor.close()
        self.assertFalse(self.monitor._thread.is_alive())