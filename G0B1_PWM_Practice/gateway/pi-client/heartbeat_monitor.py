"""Receive-only PL011 heartbeat console, 115200 8N1; no TX/GPIO/reset/GO.

Stop this console before the programmer so the exclusive UART lock is released.
Only complete, exact versioned heartbeats count; this proves liveness, not PWM health.
With --timeout alone, observe until the deadline without asserting a heartbeat count.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import termios
import time
from signed_release import version as valid_version


VERSIONS = ("0.1.0", "0.2.0", "0.3.0")
PREFIX = b"Hello from NUCLEO-G0B1RE! firmware="
MAX_LINE_BYTES = 512
DEFAULT_COUNT_TIMEOUT = 30.0


class MonitorError(Exception):
    """Invalid input stream or missed heartbeat deadline."""


def _positive_count(value):
    count = int(value)
    if count <= 0:
        raise argparse.ArgumentTypeError("count must be a positive integer")
    return count


def _positive_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return seconds


def open_port(serial_module, device):
    # Do NOT reuse the ROM opener: it deliberately configures EVEN parity.
    port = serial_module.Serial(
        port=None, baudrate=115200, bytesize=serial_module.EIGHTBITS,
        parity=serial_module.PARITY_NONE, stopbits=serial_module.STOPBITS_ONE,
        timeout=0.25, write_timeout=0.25, xonxoff=False,
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


def heartbeat_version(line, allow_dynamic=False):
    if line.startswith(PREFIX):
        try:
            version = line[len(PREFIX):].decode("ascii")
        except UnicodeError:
            return None
        if valid_version(version) and (allow_dynamic or version in VERSIONS):
            return version
    return None


def monitor(port, version=None, count=None, timeout=None, clock=time.monotonic):
    """Read at most 512 bytes per line and enforce one overall monotonic deadline."""
    if version is not None and not valid_version(version):
        raise ValueError("unknown expected version")
    if count is not None and (type(count) is not int or count <= 0):
        raise ValueError("count must be a positive integer")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be finite and positive")
    duration = DEFAULT_COUNT_TIMEOUT if count is not None and timeout is None else timeout
    deadline = None if duration is None else clock() + duration
    received = 0
    pending = bytearray()
    while count is None or received < count:
        remaining = None if deadline is None else deadline - clock()
        if remaining is not None and remaining <= 0:
            if count is None:
                return received
            raise MonitorError("heartbeat deadline expired: {}/{} matches".format(received, count))
        port.timeout = 0.25 if remaining is None else min(0.25, remaining)
        chunk = port.read(1)
        if deadline is not None and clock() >= deadline:
            if count is None:
                return received
            raise MonitorError("heartbeat deadline expired: {}/{} matches".format(received, count))
        if not chunk:
            continue
        if chunk == b"\n":
            line = bytes(pending)
            if line.endswith(b"\r"):
                line = line[:-1]
            found = heartbeat_version(line, allow_dynamic=version is not None)
            matched = found is not None and (version is None or found == version)
            timestamp = datetime.now(timezone.utc).isoformat()
            # Escape terminal controls, invalid bytes, and embedded CRs; never echo raw input.
            text = json.dumps(line.decode("ascii", errors="backslashreplace"))
            print("{} {} {}".format(timestamp, "MATCH" if matched else "MISMATCH", text), flush=True)
            received += int(matched)
            pending.clear()
        else:
            pending.extend(chunk)
            if len(pending) > MAX_LINE_BYTES:
                raise MonitorError("input line exceeds 512 bytes; stopped")
    return received


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--version", help="only count this exact numeric semver (default: any)")
    parser.add_argument("--count", type=_positive_count, help="stop after N matching lines; default: continuous")
    parser.add_argument("--timeout", type=_positive_timeout,
                        help="overall observation deadline in seconds (default: 30 with --count)")
    args = parser.parse_args(argv)
    if args.version is not None and not valid_version(args.version):
        parser.error("version must be numeric semver")
    try:
        if Path(args.port).resolve() != Path("/dev/ttyAMA0"):
            raise MonitorError("port must resolve to /dev/ttyAMA0 (PL011), not mini-UART")
        import serial
        print("Receive-only 115200 8N1. Stop this monitor before programming; no reset/GPIO/TX.", flush=True)
        try:
            with open_port(serial, args.port) as port:
                port.reset_input_buffer()
                monitor(port, args.version, args.count, args.timeout)
            return 0
        except serial.SerialException as exc:
            raise MonitorError(str(exc)) from exc
    except (MonitorError, OSError, termios.error, ImportError) as exc:
        print("ERROR: {}. Stopped; no retry.".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped; UART released.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())