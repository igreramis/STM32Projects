"""Offline regressions with real serial, PTY creation and network I/O blocked.

Four existing PTY transport tests are deliberately deferred, not deleted. Run
ordinary unittest discovery separately only when virtual serial opens are allowed.
"""
from contextlib import ExitStack
from pathlib import Path
import pty
import socket
import sys
import unittest
from unittest.mock import patch

import serial
from awscrt import mqtt


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main():
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
    selected = []
    deferred = []
    for case in cases(suite):
        if (case.__class__.__module__, case.__class__.__name__) in {
                ("test_rom_program", "PtyTests"), ("test_rom_read_id", "PseudoTerminalTests")}:
            deferred.append(case.id())
        else:
            selected.append(case)
    for name in deferred:
        print("DEFERRED (serial opens forbidden): " + name, flush=True)
    with ExitStack() as stack:
        for target, attribute in ((serial.Serial, "open"), (pty, "openpty"),
                                  (socket.socket, "connect"), (socket.socket, "connect_ex"),
                                  (socket, "getaddrinfo"), (mqtt.Connection, "connect"),
                                  (mqtt.Connection, "publish")):
            stack.enter_context(patch.object(target, attribute,
                side_effect=AssertionError("real device/network I/O forbidden in offline tests")))
        result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(selected))
    print("Executed: {}; deferred: {}".format(result.testsRun, len(deferred)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())