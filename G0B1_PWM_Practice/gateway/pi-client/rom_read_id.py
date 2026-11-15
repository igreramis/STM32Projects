"""Standalone STM32 UART ROM probe; no flash, GPIO, MQTT or reset operations.

Wire protocol: AN3155 (see ROM-READ-ID.md for reference availability).
Only sync (7F), Get (00 FF), Get Version (01 FE), Get ID (02 FD) are sent.
"""

import argparse
import json
import math
from pathlib import Path
import sys
import termios
import time


ACK = 0x79
NACK = 0x1F
EXPECTED_ID = 0x0467  # STM32G0B/G0C family, not a unique board serial number
READ_ONLY_COMMANDS = frozenset((0x00, 0x01, 0x02))


class ProbeError(Exception):
    """Communication failed or the response did not match the expected target."""


def valid_timeout(value):
    number = float(value)
    if not math.isfinite(number) or not 0.1 <= number <= 10.0:
        raise argparse.ArgumentTypeError("timeout must be between 0.1 and 10 seconds")
    return number


class RomProbe:
    def __init__(self, port, timeout=2.0, clock=time.monotonic):
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 10.0:
            raise ValueError("timeout must be between 0.1 and 10 seconds")
        self.port = port
        self.timeout = timeout
        self.clock = clock

    def _read(self, count, deadline, stage):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProbeError("{}: response deadline expired".format(stage))
            self.port.timeout = remaining
            chunk = self.port.read(count - len(data))
            if not chunk or self.clock() > deadline:
                raise ProbeError("{}: timed out after {}/{} bytes".format(
                    stage, len(data), count))
            data.extend(chunk)
        return bytes(data)

    def _ack(self, deadline, stage):
        value = self._read(1, deadline, stage)[0]
        if value == NACK:
            raise ProbeError("{}: ROM returned NACK".format(stage))
        if value != ACK:
            raise ProbeError("{}: expected ACK 0x79, received 0x{:02X}".format(
                stage, value))

    def _send(self, data):
        self.port.write_timeout = self.timeout
        if self.port.write(data) != len(data):
            raise ProbeError("short serial write; stop and reset before retrying")
        # Do not use flush()/tcdrain(): these can wait indefinitely on some ports.

    def _command(self, command):
        if command not in READ_ONLY_COMMANDS:
            raise ProbeError("command is not in the read-only allowlist")
        self._send(bytes((command, command ^ 0xFF)))
        deadline = self.clock() + self.timeout
        self._ack(deadline, "command 0x{:02X}".format(command))
        return deadline

    def sync(self):
        self._send(b"\x7f")
        self._ack(self.clock() + self.timeout, "sync")

    def run(self):
        self.sync()

        deadline = self._command(0x00)
        count = self._read(1, deadline, "Get length")[0] + 1
        payload = self._read(count, deadline, "Get payload")
        self._ack(deadline, "Get trailing ACK")
        version, commands = payload[0], payload[1:]
        if not READ_ONLY_COMMANDS.issubset(commands):
            raise ProbeError("Get did not advertise all required read-only commands")

        deadline = self._command(0x01)
        version_reply = self._read(3, deadline, "Get Version payload")
        self._ack(deadline, "Get Version trailing ACK")
        if version_reply[0] != version:
            raise ProbeError("Get and Get Version disagree on ROM version")

        deadline = self._command(0x02)
        count = self._read(1, deadline, "Get ID length")[0] + 1
        if count != 2:
            raise ProbeError("expected a two-byte STM32G0 device ID, got {}".format(count))
        device_id = int.from_bytes(self._read(2, deadline, "Get ID payload"), "big")
        self._ack(deadline, "Get ID trailing ACK")
        if device_id != EXPECTED_ID:
            raise ProbeError("wrong device family: 0x{:04X}; expected 0x0467".format(device_id))
        return {
            "status": "rom_identified",
            "device_id": "0x{:04X}".format(device_id),
            "bootloader_version_raw": "0x{:02X}".format(version),
            "bootloader_version": "{}.{}".format(version >> 4, version & 15),
            "supported_commands": ["0x{:02X}".format(c) for c in commands],
            # Preserve raw bytes; do not infer option-byte RDP state from them.
            "version_status_bytes": list(version_reply[1:]),
            "flashed": False,
            "application_health_verified": False,
        }


def open_port(serial_module, device, baud, timeout):
    # Construct closed: configure framing and modem lines BEFORE opening.
    port = serial_module.Serial(
        port=None, baudrate=baud, bytesize=serial_module.EIGHTBITS,
        parity=serial_module.PARITY_EVEN, stopbits=serial_module.STOPBITS_ONE,
        timeout=timeout, write_timeout=timeout, xonxoff=False,
        rtscts=False, dsrdtr=False, exclusive=True,
    )
    try:
        port.dtr = False
        port.rts = False
        port.port = device
        port.open()
        return port
    except BaseException:
        port.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--baud", type=int, choices=(9600, 19200, 38400, 57600, 115200), default=115200)
    parser.add_argument("--timeout", type=valid_timeout, default=2.0,
                        help="per-command response deadline, seconds (default: 2)")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print("ERROR: interactive terminal required for manual reset confirmation", file=sys.stderr)
        return 2
    # Target-specific guard: serial0 must resolve to the configured PL011 tty.
    if Path(args.port).resolve() != Path("/dev/ttyAMA0"):
        print("ERROR: port must resolve to /dev/ttyAMA0 (PL011), not mini-UART", file=sys.stderr)
        return 2
    try:
        import serial
    except ImportError:
        print("ERROR: install the separate hardware requirements (pyserial)", file=sys.stderr)
        return 2

    print("Read-ID only. No GPIO control, firmware write, erase, unprotect or GO.")
    print("Before continuing: both boards powered; approved UART/GND wiring;")
    print("CN4 removed; BOOT0 held at target 3.3 V; full target power cycle after SWD.")
    print("Keep ST-LINK VCP and every other serial program idle/closed.")
    try:
        if input("Type READY to open PL011 (no bytes sent yet): ").strip() != "READY":
            print("Cancelled; serial port not opened.")
            return 2
        with open_port(serial, args.port, args.baud, args.timeout) as port:
            # Clear stale input BEFORE B2, never discard the ROM's response.
            port.reset_input_buffer()
            if input("Press and release B2 RESET now; then type RESET: ").strip() != "RESET":
                print("Cancelled; no protocol bytes sent.")
                return 2
            result = RomProbe(port, args.timeout).run()
            print(json.dumps(result, indent=2))
        print("ROM identified only. This does not prove a firmware update.")
        return 0
    except (ProbeError, serial.SerialException, OSError, termios.error) as exc:
        print("ERROR: {}. No automatic retry; inspect wiring and reset before retrying.".format(exc),
              file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130
    finally:
        print("No BOOT0/reset state was changed by this helper. Leave CN4 removed.")
        print("To return to the app, follow the powered-off BOOT0-to-GND procedure.")


if __name__ == "__main__":
    sys.exit(main())