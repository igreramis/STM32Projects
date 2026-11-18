"""Known unresolved sync cases, reproduced without hardware or network.

Expected failures document desired behavior that the current receiver lacks;
they are not evidence that either case caused a particular hardware failure.
"""
import contextlib
import io
import unittest

import ota_installer as ota
from test_ota_installer import Clock, SyncPort


class TransitionCases(unittest.TestCase):
    @unittest.expectedFailure
    def test_application_suffix_without_rom_must_not_synchronize(self):
        clock = Clock()
        port = SyncPort(clock, ready=0,
                        response=b'type":"state","state":"ready"}\r\n')
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ota.InstallerError):
                ota.wait_for_rom(port, clock)

    @unittest.expectedFailure
    def test_ack_after_empty_drain_must_not_send_second_sync(self):
        clock = Clock()
        port = SyncPort(clock)
        original = port.read
        injected = False

        def read(count):
            nonlocal injected
            value = original(count)
            # ACK arrives after the nonblocking syscall returned no bytes,
            # before the following write. No wire timing is asserted here.
            if clock() == 2 and port.timeout == 0 and not value and not injected:
                port.pending.extend(b'\x79')
                injected = True
            return value

        port.read = read
        with contextlib.redirect_stdout(io.StringIO()):
            ota.wait_for_rom(port, clock)
        self.assertTrue(injected)
        self.assertEqual(port.writes, [b'\x7f'])


if __name__ == '__main__':
    unittest.main()