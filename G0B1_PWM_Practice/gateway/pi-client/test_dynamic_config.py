"""Local opt-in configuration only, with ephemeral public test keys."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ota_jobs
import uart_state
from test_signed_release import KEY_ID, key_fixture


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        key_fixture(self.root)
        self.path = self.root / "test-config.json"
        self.config = dict(catalog="catalog.json", release_root="images", baseline="baseline.bin",
                           run_root="runs", state_root="jobs", port="/dev/ttyAMA0")

    def load(self):
        self.path.write_text(json.dumps(self.config))
        jobs = ota_jobs.from_config(self.path, "pi-lab-01")
        self.addCleanup(jobs.close)
        return jobs

    def signing(self):
        return dict(public_key="test-public.pem", key_id=KEY_ID, bucket="test-firmware-bucket",
                    region="ap-southeast-2", seed_version="0.1.0")

    def test_legacy_configuration_no_monitor_or_signing(self):
        jobs = self.load()
        self.assertIsNone(jobs.monitor)
        self.assertIsNone(jobs.installer.verifier)
        self.assertIsNone(jobs.installer.predecessor_state)

    def test_explicit_signing_and_monitor_never_open_on_configuration(self):
        self.config.update(signed_releases=self.signing(), uart_monitor=True)
        with patch.object(uart_state.StateMonitor, "_open", side_effect=AssertionError("no UART")) as opened:
            jobs = self.load()
            opened.assert_not_called()
        self.assertIsInstance(jobs.installer, uart_state.MonitoredInstaller)
        self.assertIsNone(jobs.monitor._thread)
        state = jobs.installer.installer.predecessor_state
        self.assertEqual(state.root, self.root / "jobs" / "verified-flash")
        self.assertFalse(state.root.exists())

    def test_bad_config_fails_closed(self):
        for extra in [dict(uart_monitor="true"), dict(uart_monitor=1), dict(signed_releases=None),
                      dict(signed_releases=dict(self.signing(), seed_version="7.8.9")),
                      dict(signed_releases=dict(self.signing(), key_id="alias/remote")),
                      dict(signed_releases=dict(self.signing(), extra=1))]:
            old = dict(self.config)
            self.config.update(extra)
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "invalid_ota_config"):
                self.load()
            self.config = old

    def test_dynamic_command_requires_envelope_url_and_bound(self):
        command = dict(command_id="test", command="install_firmware", version="7.8.9", from_version="0.1.0",
                       sha256="a" * 64, expires_at=1500, release={}, url="https://example.invalid")
        ota_jobs.OtaJobs._command(command)
        for changes in ({"url": "x" * 3501}, {"release": {"payload": "x" * 8192}},
                        {"version": "07.8.9"}, {"from_version": "device"}):
            with self.assertRaises(ValueError):
                ota_jobs.OtaJobs._command(dict(command, **changes))
        for field in ("release", "url"):
            with self.assertRaises(ValueError):
                ota_jobs.OtaJobs._command({k: v for k, v in command.items() if k != field})

    def test_close_stops_monitor_while_lifetime_lock_is_still_held(self):
        jobs = self.load()
        monitor = Mock()
        jobs.monitor = monitor

        def close_monitor():
            with self.assertRaisesRegex(RuntimeError, "ota_state_locked"):
                ota_jobs.OtaJobs("pi-lab-01", jobs.state_root, Mock())

        monitor.close.side_effect = close_monitor
        jobs.close()
        monitor.close.assert_called_once()
        replacement = ota_jobs.OtaJobs("pi-lab-01", jobs.state_root, Mock())
        replacement.close()

    def test_failed_monitor_close_keeps_lifetime_lock(self):
        jobs = self.load()
        monitor = Mock()
        jobs.monitor = monitor
        monitor.close.side_effect = RuntimeError("close failed")
        try:
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                jobs.close()
            with self.assertRaisesRegex(RuntimeError, "ota_state_locked"):
                ota_jobs.OtaJobs("pi-lab-01", jobs.state_root, Mock())
        finally:
            monitor.close.side_effect = None

    def test_signed_example_loads_only_after_local_pin_and_seed_provisioning(self):
        example = Path(__file__).with_name("ota-config.signed.example.json")
        self.config = json.loads(example.read_text())
        self.config["port"] = "/dev/ttyAMA0"
        with self.assertRaisesRegex(ValueError, "invalid_ota_config"):
            self.load()
        self.config["signed_releases"] = self.signing()
        jobs = self.load()
        self.assertIsInstance(jobs.installer, uart_state.MonitoredInstaller)
        self.assertEqual(jobs.installer.installer.predecessor_state.seed_version, "0.1.0")
        self.assertIsNone(jobs.monitor._thread)