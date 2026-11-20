"""Local, single-use reset confirmation for one private run; no UART access.

The operator must release B2 with BOOT0 HIGH BEFORE invoking this CLI. A fresh
Unix socket exists only while that run is waiting. No queued files, MQTT command,
stdin on the daemon, or persistent confirmation can release a subsequent run.
"""
import argparse
from pathlib import Path
import socket
import time


class ConfirmationTimeout(Exception):
    pass


def wait_for_reset(record, authorize, stop_event, timeout=120.0):
    path = Path(record.path) / 'reset.sock'
    authorize()
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        # Never unlink a pre-existing endpoint: a reused run must fail closed.
        server.bind(str(path))
        path.chmod(0o600)
        try:
            server.listen(1)
            record.event('waiting-for-reset-confirmation')
            print('No UART sync sent. Set BOOT0 HIGH, press/release B2, then confirm this run: '
                  + str(record.path), flush=True)
            while True:
                authorize()
                if stop_event is not None and stop_event.is_set():
                    raise ConfirmationTimeout()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConfirmationTimeout()
                server.settimeout(min(0.25, remaining))
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(min(0.25, max(0.001, deadline - time.monotonic())))
                    # A short/malformed request fails this run, never triggers TX.
                    request = bytearray()
                    while len(request) < 6:
                        part = connection.recv(6 - len(request))
                        if not part:
                            break
                        request.extend(part)
                    authorize()
                    if stop_event is not None and stop_event.is_set():
                        raise ConfirmationTimeout()
                    if time.monotonic() >= deadline:
                        raise ConfirmationTimeout()
                    if request != b'RESET\n':
                        raise ValueError('invalid_reset_confirmation')
                    record.event('reset-confirmed')
                    connection.sendall(b'OK\n')
                    return True
        finally:
            path.unlink()


def confirm_reset(run_directory):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(str(Path(run_directory) / 'reset.sock'))
        client.sendall(b'RESET\n')
        reply = bytearray()
        while len(reply) < 3:
            part = client.recv(3 - len(reply))
            if not part:
                break
            reply.extend(part)
        if reply != b'OK\n':
            raise ValueError('reset_confirmation_failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-directory', required=True)
    parser.add_argument('--reset-released', action='store_true', required=True,
                        help='Attest BOOT0 HIGH and B2 already pressed/released.')
    args = parser.parse_args()
    try:
        confirm_reset(args.run_directory)
    except (OSError, ValueError):
        print('Reset confirmation failed; no automatic retry.')
        return 1
    print('Reset confirmation accepted for this run only.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())