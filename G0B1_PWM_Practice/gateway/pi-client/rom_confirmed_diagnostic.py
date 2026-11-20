"""One reset-confirmed, read-only identity trial; no MQTT, erase, write or GO."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from ota_installer import ConfirmedRomProbe, InstallerError, _check_stop
from reset_confirmation import wait_for_reset
from rom_read_id import open_port


class LocalRecord:
    def __init__(self, root):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix='read-id-', dir=str(root)))

    def event(self, name, **details):
        fd = os.open(str(self.path / 'events.jsonl'),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            log.write(json.dumps(dict(event=name, **details)) + '\n')
            log.flush()
            os.fsync(log.fileno())


class ReadOnlyPort:
    """Enforce exactly one sync, Get, Get Version, Get ID, in that order."""
    def __init__(self, port):
        object.__setattr__(self, 'port', port)
        object.__setattr__(self, 'step', 0)
        object.__setattr__(self, 'trace', [])

    def __getattr__(self, name):
        return getattr(self.port, name)

    def __setattr__(self, name, value):
        setattr(self.port, name, value)

    def reset_input_buffer(self):
        if self.step != 0:
            raise RuntimeError('post_sync_clear_forbidden')
        self.port.reset_input_buffer()
        self.trace.append(dict(operation='clear-rx', monotonic=time.monotonic()))

    def write(self, data):
        commands = (b'\x7f', b'\x00\xff', b'\x01\xfe', b'\x02\xfd')
        labels = ('sync', 'get', 'get-version', 'get-id')
        if self.step >= len(commands) or data != commands[self.step]:
            raise RuntimeError('read_only_guard')
        self.trace.append(dict(operation=labels[self.step], monotonic=time.monotonic()))
        object.__setattr__(self, 'step', self.step + 1)
        return self.port.write(data)


def main():
    import serial
    root = Path('/home/pi/aws-iot')
    config = json.loads((root / 'ota-config.json').read_text())
    fd = os.open(str(root / config['state_root']), os.O_RDONLY | os.O_DIRECTORY)
    stop = threading.Event()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if subprocess.check_output(['systemctl', 'show', 'pi-iot-ota.service',
                                    '-p', 'ActiveState', '--value']).strip() != b'inactive':
            raise RuntimeError('service_must_be_inactive')
        if Path('/dev/serial0').resolve() != Path('/dev/ttyAMA0'):
            raise RuntimeError('PL011_required')
        record = LocalRecord(root / 'rom-diagnostic-runs')
        print('Read-only confirmed-reset run: ' + str(record.path), flush=True)
        with open_port(serial, '/dev/serial0', 115200, 2.0) as port:
            guarded = ReadOnlyPort(port)
            probe = ConfirmedRomProbe(guarded,
                lambda: wait_for_reset(record, lambda: _check_stop(stop), stop),
                stop_event=stop)
            probe.record = record
            try:
                identity = probe.run()
            except InstallerError as exc:
                record.event('sync-failed', reason=exc.code)
                print('Read-only trial stopped: ' + exc.code, flush=True)
                return 1
            except Exception:
                print('Read-only trial stopped; see local diagnostic record.', flush=True)
                return 1
            finally:
                record.event('host-tx-trace', events=guarded.trace)
            record.event('rom-identified', device_id=identity['device_id'],
                         version=identity['bootloader_version_raw'])
            print('Read-only identity validated: ' + identity['device_id'], flush=True)
            return 0
    finally:
        os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())