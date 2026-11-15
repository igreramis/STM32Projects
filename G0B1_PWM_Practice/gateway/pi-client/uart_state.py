"""Receive-only UART state observations and synchronous installer handoff.

No cloud publication or UART TX. Snapshot calls never create observations.
Close/open failures inhibit handoff/reopening rather than risking two owners.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

import heartbeat_monitor
from signed_release import unique, version

DEVICE = "stm32g0b1-01"
STATES = {"idle", "ready", "running", "paused", "fault"}
STALE_SECONDS = 35


def parse_line(line):
    if len(line) > 512:
        return None
    firmware = heartbeat_monitor.heartbeat_version(line, allow_dynamic=True)
    if firmware is not None:
        return dict(firmware=firmware, state=None, sequence=None)
    try:
        sample = json.loads(line.decode("ascii"), object_pairs_hook=unique)
        if (type(sample) is not dict or set(sample) != {"type", "device_id", "firmware", "state", "sequence"} or
                sample["type"] != "state" or sample["device_id"] != DEVICE or
                not version(sample["firmware"]) or sample["state"] not in STATES or
                type(sample["sequence"]) is not int or not 0 <= sample["sequence"] <= 0xffffffff):
            return None
        return {key: sample[key] for key in ("firmware", "state", "sequence")}
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return None


class StateMonitor:
    def __init__(self, port, clock=time.monotonic, wall_clock=time.time, opener=None):
        if Path(port).resolve() != Path("/dev/ttyAMA0"):
            raise ValueError("monitor_requires_pl011")
        self.port, self.clock, self.wall_clock = port, clock, wall_clock
        self.opener = opener or self._open
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._halt = threading.Event()
        self._thread = None
        self._closed = False
        self._released = True
        self._updating = False
        self._sample = None
        self._state_seen = None
        self._seen = self._utc = None
        self._pending = bytearray()
        self._discard = False

    @staticmethod
    def _open(port):
        import serial
        return heartbeat_monitor.open_port(serial, port)

    def feed(self, chunk):
        """Bounded stream parser; discard an entire oversized line, not its tail."""
        with self._lock:
            for value in chunk:
                if value == 10:
                    raw = bytes(self._pending)
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    sample = None if self._discard else parse_line(raw)
                    self._pending.clear()
                    self._discard = False
                    if sample is not None:
                        if sample["sequence"] is not None:
                            # The counter can repeat after reboot; it is not a
                            # replay identifier. Every validated UART line counts.
                            self._state_seen = self.clock()
                        elif self._sample is not None and sample["firmware"] == self._sample["firmware"]:
                            sample = dict(self._sample)
                        else:
                            self._state_seen = None
                        self._sample = sample
                        self._seen = self.clock()
                        self._utc = datetime.fromtimestamp(self.wall_clock(), timezone.utc).isoformat()
                elif not self._discard:
                    self._pending.append(value)
                    if len(self._pending) > 512:
                        self._pending.clear()
                        self._discard = True

    def snapshot(self):
        with self._lock:
            sample = dict(self._sample or dict(firmware=None, state=None, sequence=None))
            # Heartbeats prove liveness, not operating-state freshness.
            if self._state_seen is None or self.clock() - self._state_seen >= STALE_SECONDS:
                sample.update(state=None, sequence=None)
            return dict(sample, device_id=DEVICE, last_seen=self._utc,
                        age_seconds=None if self._seen is None else max(0.0, self.clock() - self._seen),
                        updating=self._updating)

    def _run(self):
        port = None
        try:
            self._released = False
            port = self.opener(self.port)
            port.reset_input_buffer()
            while not self._halt.is_set():
                port.timeout = 0.25
                chunk = port.read(1)
                if not self._halt.is_set():
                    self.feed(chunk)
        except Exception:
            # Fail closed, never log UART or exception text; no retry loop.
            pass
        finally:
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
                else:
                    self._released = True

    def start(self):
        with self._lifecycle:
            if self._closed or not self._released or (self._thread is not None and self._thread.is_alive()):
                return
            self._halt.clear()
            with self._lock:
                self._pending.clear()
                self._discard = False
                self._state_seen = None  # A reset may have occurred during handoff.
            self._thread = threading.Thread(target=self._run, name="uart-state", daemon=False)
            self._thread.start()

    def pause(self):
        with self._lifecycle:
            with self._lock:
                self._updating = True
            self._halt.set()
            if self._thread is not None:
                self._thread.join()  # No UART may open until close completed.
            if not self._released:
                with self._lock:
                    self._updating = False
                raise RuntimeError("uart_not_released")

    def resume(self, safe=True):
        with self._lifecycle:
            with self._lock:
                self._updating = False
            if safe:
                self.start()
            else:
                self._released = False

    def close(self):
        with self._lifecycle:
            self._closed = True
            self._halt.set()
            if self._thread is not None:
                self._thread.join()
            if not self._released:
                raise RuntimeError("uart_not_released")


class MonitoredInstaller:
    def __init__(self, installer, monitor):
        self.installer, self.monitor = installer, monitor

    def __call__(self, command, progress, stop_event):
        self.monitor.pause()  # synchronous, before download or any worker open
        try:
            return self.installer(command, progress, stop_event)
        finally:
            self.monitor.resume(safe=self.installer.uart_released)