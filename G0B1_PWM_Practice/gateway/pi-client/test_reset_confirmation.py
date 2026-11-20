"""Reset-gated sync, private local IPC, and read-only guard; no hardware."""
import contextlib
import io
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock

import ota_installer as ota
import reset_confirmation as reset
from rom_confirmed_diagnostic import ReadOnlyPort
from test_ota_installer import Clock, SyncPort, InstallerFixture
from test_rom_read_id import FakePort, REPLY, TX


class ConfirmedSyncTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = SyncPort(self.clock, ready=0)
        self.stop = threading.Event()
        self.redirect = contextlib.redirect_stdout(io.StringIO())
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def probe(self, confirm):
        return ota.ConfirmedRomProbe(self.port, confirm, self.clock, self.stop)

    def test_no_io_until_confirmation_then_clear_then_one_sync(self):
        def confirm():
            self.assertEqual(self.port.writes, [])
            self.assertEqual(self.port.clears, 0)
            self.assertEqual(self.port.read_timeouts, [])
            self.clock.now += 30
            self.port.pending.extend(b'type":"state","state":"ready"}\n')
            return True
        self.probe(confirm).sync()
        self.assertEqual(self.port.clears, 1)
        self.assertEqual(self.port.write_times, [30])
        self.assertEqual(self.port.writes, [b'\x7f'])

    def test_no_ack_sends_once_only(self):
        self.port.ready = None
        with self.assertRaises(ota.InstallerError) as caught:
            self.probe(lambda: True).sync()
        self.assertEqual(caught.exception.code, 'bootloader_timeout')
        self.assertEqual(self.port.writes, [b'\x7f'])
        self.assertEqual(self.clock(), 2)

    def test_noise_suffix_and_nack_fail_without_scanning_for_y(self):
        for response in (b'type":"state"', b'\x1f', b'\x00'):
            with self.subTest(response=response):
                self.port = SyncPort(self.clock, ready=0, response=response)
                with self.assertRaises(ota.InstallerError):
                    self.probe(lambda: True).sync()
                self.assertEqual(self.port.writes, [b'\x7f'])

    def test_rejected_cancelled_or_failed_confirmation_never_writes(self):
        for action in ('reject', 'cancel', 'timeout', 'expire', 'transport'):
            with self.subTest(action=action):
                self.setUpPort()
                def confirm():
                    if action == 'cancel':
                        self.stop.set()
                        return True
                    if action == 'timeout':
                        raise reset.ConfirmationTimeout()
                    if action == 'expire':
                        raise ota.InstallerError('command_expired')
                    if action == 'transport':
                        raise OSError('private details')
                    return False
                with self.assertRaises(ota.InstallerError):
                    self.probe(confirm).sync()
                self.assertEqual(self.port.writes, [])
                self.assertEqual(self.port.clears, 0)

    def setUpPort(self):
        self.port = SyncPort(self.clock, ready=0)
        self.stop.clear()

    def test_repeat_sync_rejected_and_identity_transcript_unchanged(self):
        raw = FakePort(REPLY, fragment=1)
        raw.reset_input_buffer = Mock()
        guarded = ReadOnlyPort(raw)
        probe = ota.ConfirmedRomProbe(guarded, lambda: True)
        self.assertEqual(probe.run()['device_id'], '0x0467')
        self.assertEqual(raw.writes, TX)
        with self.assertRaises(ota.InstallerError):
            probe.sync()
        self.assertEqual(raw.writes, TX)

    def test_guard_blocks_repeated_sync_and_destructive_commands(self):
        for packet in (b'\x44\xbb', b'\x31\xce', b'\x21\xde', b'\x7f'):
            raw = Mock()
            port = ReadOnlyPort(raw)
            port.write(b'\x7f')
            with self.assertRaises(RuntimeError):
                port.write(packet)
            self.assertEqual(raw.write.call_count, 1)


class ResetSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.record = Mock(path=self.path)
        self.record.event.side_effect = lambda name: self.ready.set() if name == 'waiting-for-reset-confirmation' else None
        self.result = []

    def start(self, timeout=2):
        def run():
            try:
                self.result.append(reset.wait_for_reset(self.record, self.authorize, self.stop, timeout))
            except Exception as exc:
                self.result.append(exc)
        self.thread = threading.Thread(target=run)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.assertTrue(self.ready.wait(1))

    def authorize(self):
        ota._check_stop(self.stop)

    def test_single_use_endpoint_and_no_stale_confirmation(self):
        with self.assertRaises(OSError):
            reset.confirm_reset(self.path)
        self.start()
        self.assertEqual((self.path / 'reset.sock').stat().st_mode & 0o777, 0o600)
        reset.confirm_reset(self.path)
        self.thread.join(3)
        self.assertEqual(self.result, [True])
        self.assertFalse((self.path / 'reset.sock').exists())
        with self.assertRaises(OSError):
            reset.confirm_reset(self.path)
        self.record.event.assert_any_call('reset-confirmed')

    def test_timeout_cleans_endpoint(self):
        self.start(timeout=0.05)
        self.thread.join(3)
        self.assertIsInstance(self.result[0], reset.ConfirmationTimeout)
        self.assertFalse((self.path / 'reset.sock').exists())

    def test_cancel_cleans_endpoint(self):
        self.start()
        self.stop.set()
        self.thread.join(3)
        self.assertIsInstance(self.result[0], ota.InstallerError)
        self.assertEqual(self.result[0].code, 'cancelled')
        self.assertFalse((self.path / 'reset.sock').exists())

    def test_invalid_confirmation_never_accepts(self):
        self.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.path / 'reset.sock'))
            client.sendall(b'WRONG\n')
        self.thread.join(3)
        self.assertIsInstance(self.result[0], ValueError)
        self.assertFalse((self.path / 'reset.sock').exists())


class InstallerGateTests(InstallerFixture):
    def test_production_gate_invoked_before_any_uart_packet(self):
        def confirm(record, authorize, stop):
            self.assertEqual(self.rom.packets, [])
            self.assertTrue(record.path.is_dir())
            authorize()
            return True
        self.installer.reset_confirmation = confirm
        self.assertEqual(self.run_installer()['status'], 'application_verified')

    def test_gate_cancellation_no_flash_or_app_open(self):
        def confirm(record, authorize, stop):
            stop.set()
            authorize()
        self.installer.reset_confirmation = confirm
        self.assert_error(self.run_installer(), 'cancelled')
        self.assertEqual(self.rom.packets, [])
        self.assertFalse(self.app.opened)


if __name__ == '__main__':
    unittest.main()