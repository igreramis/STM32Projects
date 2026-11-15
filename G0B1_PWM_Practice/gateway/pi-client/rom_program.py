"""Fixed-image, supervised STM32G0B1RE ROM programmer. Default is OFFLINE.

No GPIO, GO, mass erase, protection changes, cloud integration or retry.
See ROM-PROGRAM.md before the separately authorized --program operation.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import termios
import time

from rom_read_id import ProbeError, RomProbe, open_port


FLASH_BASE = 0x08000000
FLASH_SIZE = 512 * 1024
PAGE_SIZE = 2048
PAGES = tuple(range(10))
ERASE_SIZE = PAGE_SIZE * len(PAGES)
IMAGE_SIZE = 19212
IMAGE_SHA256 = "e2c6d4bcf172cc803fa497a60bd0b625f5448a33433ab2d00bacec5fff6cc521"
BASELINE_SHA256 = "e46721beb7d9465aeb237c76f6dfa1bcfd584c4986578183bc7c66f582c9a5a5"
PADDED_SIZE = (IMAGE_SIZE + 7) & ~7
FLASH_SIZE_REGISTER = 0x1FFF75E0
# RM0444 Rev6 Tables16/17: stored option word + complemented word.
# UART ROM rejects peripheral FLASH_OPTR at 0x40022020 on this device.
OPTION_BYTES_ADDRESS = 0x1FFF7800
EXPECTED_OPTR = 0xFEFFFEAA
MEMORY_COMMANDS = frozenset((0x11, 0x31, 0x44))
CONFIRMATION = "ERASE 0-9 WRITE " + IMAGE_SHA256


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_vectors(image):
    if len(image) < 8:
        raise ProbeError("image lacks a vector table")
    sp = int.from_bytes(image[:4], "little")
    reset = int.from_bytes(image[4:8], "little")
    if sp % 8 or not 0x20000000 < sp <= 0x20024000:
        raise ProbeError("initial stack pointer outside aligned 144 KiB SRAM")
    if not reset & 1 or not FLASH_BASE <= (reset & ~1) < FLASH_BASE + len(image):
        raise ProbeError("reset vector is not Thumb code inside the image")


def validate_image(data):
    # Own immutable bytes for the entire operation; never re-read after approval.
    image = bytes(data)
    if len(image) != IMAGE_SIZE or sha256(image) != IMAGE_SHA256:
        raise ProbeError("image does not match the fixed approved size/SHA-256")
    validate_vectors(image)
    return image


def load_image(path):
    with Path(path).open("rb") as source:
        return validate_image(source.read(IMAGE_SIZE + 1))


def xor_packet(payload):
    checksum = 0
    for value in payload:
        checksum ^= value
    return payload + bytes((checksum,))


def erase_packet(pages):
    if tuple(pages) != PAGES:
        raise ProbeError("only the exact ordered page list 0-9 is allowed")
    return xor_packet((len(PAGES) - 1).to_bytes(2, "big") +
                      b"".join(page.to_bytes(2, "big") for page in PAGES))


def write_packet(address, data):
    if (not 1 <= len(data) <= 256 or len(data) % 8 or address % 8 or
            address < FLASH_BASE or address + len(data) > FLASH_BASE + PADDED_SIZE):
        raise ProbeError("write outside fixed image range or invalid 8-byte alignment")
    return xor_packet(bytes((len(data) - 1,)) + data)


class RunRecord:
    """Private, unique run directory; fsync backups and intent before erase."""

    def __init__(self, root):
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="rom-", dir=str(root)))
        self._sync_dir(root)
        self.event("created", image_sha256=IMAGE_SHA256, baseline_sha256=BASELINE_SHA256)

    @staticmethod
    def _sync_dir(path):
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def event(self, name, **details):
        entry = {"utc": datetime.now(timezone.utc).isoformat(), "event": name, **details}
        fd = os.open(str(self.path / "events.jsonl"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as log:
            log.write(json.dumps(entry, sort_keys=True) + "\n")
            log.flush()
            os.fsync(log.fileno())
        self._sync_dir(self.path)

    def snapshot(self, name, data):
        if name not in ("flash-before.bin", "flash-after.bin") or len(data) != FLASH_SIZE:
            raise ProbeError("snapshot must be an entire flash image with a fixed name")
        path = self.path / name
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            if output.write(data) != len(data):
                raise OSError("short backup write")
            output.flush()
            os.fsync(output.fileno())
        self._sync_dir(self.path)
        if path.read_bytes() != data:
            raise ProbeError("saved snapshot verification failed")
        self.event("snapshot", file=name, size=len(data), sha256=sha256(data))


class Programmer:
    """One-use session. Private destructive methods require ordered state gates.

    Composition deliberately leaves RomProbe's read-only command allowlist intact.
    """

    def __init__(self, port, image, record, clock=time.monotonic, diagnostic_only=False):
        self.image = validate_image(image)
        self.padded = self.image + b"\xff" * (PADDED_SIZE - IMAGE_SIZE)
        self.io = RomProbe(port, timeout=2.0, clock=clock)
        self.record = record
        self.clock = clock
        self.phase = "new"
        self.destructive_started = False
        self._write_offset = 0
        self.diagnostic_only = diagnostic_only

    def _command(self, command, deadline):
        if self.diagnostic_only and command != 0x11:
            raise ProbeError("diagnostic mode permits Read Memory only")
        if command not in MEMORY_COMMANDS:
            raise ProbeError("command outside programmer memory allowlist")
        if command == 0x44 and self.phase != "erase-authorized":
            raise ProbeError("erase not authorized")
        if command == 0x31 and self.phase != "erased-verified":
            raise ProbeError("write requires verified erase")
        self.io._send(bytes((command, command ^ 0xFF)))
        self.io._ack(deadline, "command 0x{:02x}".format(command))

    def _address(self, address, deadline):
        self.io._send(xor_packet(address.to_bytes(4, "big")))
        self.io._ack(deadline, "address 0x{:08X}".format(address))

    def read_memory(self, address, count):
        flash = FLASH_BASE <= address and address + count <= FLASH_BASE + FLASH_SIZE
        register = (address, count) in ((FLASH_SIZE_REGISTER, 2), (OPTION_BYTES_ADDRESS, 8))
        if not 1 <= count <= 256 or not (flash or register):
            raise ProbeError("read outside approved flash/geometry registers")
        deadline = self.clock() + 2.0
        self._command(0x11, deadline)
        self._address(address, deadline)
        self.io._send(bytes((count - 1, (count - 1) ^ 0xFF)))
        self.io._ack(deadline, "read length")
        # Read Memory has no trailing ACK after the data.
        return self.io._read(count, deadline, "read data")

    def _read_flash(self, size):
        deadline = self.clock() + 300.0
        result = bytearray()
        while len(result) < size:
            if self.clock() >= deadline:
                raise ProbeError("whole flash read deadline expired")
            result.extend(self.read_memory(FLASH_BASE + len(result), min(256, size - len(result))))
        if self.clock() > deadline:
            raise ProbeError("whole flash read completed after deadline")
        return bytes(result)

    def _identify(self):
        identity = self.io.run()
        if (identity["bootloader_version_raw"] != "0x31" or
                not {"0x11", "0x31", "0x44"}.issubset(identity["supported_commands"])):
            raise ProbeError("ROM version/capabilities differ from the tested target")
        self.record.event("rom-identity", identity=identity)
        values = []
        for name, address, count in (("flash-size", FLASH_SIZE_REGISTER, 2),
                                     ("stored-options", OPTION_BYTES_ADDRESS, 8)):
            print("Reading {} at 0x{:08X} ({} bytes)...".format(name, address, count), flush=True)
            self.record.event("geometry-read-start", register=name, address=hex(address), count=count)
            raw = self.read_memory(address, count)
            if address == OPTION_BYTES_ADDRESS:
                value = int.from_bytes(raw[:4], "little")
                complement = int.from_bytes(raw[4:], "little")
                if value ^ complement != 0xFFFFFFFF:
                    raise ProbeError("stored option word/complement mismatch; stop")
            else:
                value = int.from_bytes(raw, "little")
            values.append(value)
            self.record.event("geometry-read-ok", register=name, address=hex(address), value=hex(value))
            print("{} = 0x{:X}".format(name, value), flush=True)
        size, optr = values
        if size != 512 or optr != EXPECTED_OPTR:
            raise ProbeError("flash size/options changed; require 512 KiB and OPTR 0xFEFFFEAA")
        self.record.event("identified", identity=identity, flash_kib=size, optr=hex(optr))

    def _erase(self):
        packet = erase_packet(PAGES)
        # Once intent is logged, even an ACK timeout may mean flash was modified.
        self.record.event("erase-intent", pages=list(PAGES))
        self.destructive_started = True
        deadline = self.clock() + 30.0
        self._command(0x44, deadline)
        self.io._send(packet)
        self.io._ack(deadline, "page erase")
        self.phase = "erase-acknowledged"
        if self._read_flash(ERASE_SIZE) != b"\xff" * ERASE_SIZE:
            raise ProbeError("erased pages are not all 0xFF")
        self.phase = "erased-verified"
        self.record.event(self.phase)

    def _write_block(self, offset, data):
        if (offset != self._write_offset or data != self.padded[offset:offset + len(data)]):
            raise ProbeError("write must be the next exact approved image block")
        address = FLASH_BASE + offset
        packet = write_packet(address, data)
        deadline = self.clock() + 5.0
        self._command(0x31, deadline)
        self._address(address, deadline)
        self.io._send(packet)
        self.io._ack(deadline, "write data")
        self._write_offset += len(data)

    def run(self, confirm):
        if self.phase != "new":
            raise ProbeError("session is single-use; no automatic retry")
        self.phase = "identifying"
        try:
            self._identify()
            if self.diagnostic_only:
                self.phase = "diagnosed"
                result = {"status": "geometry_verified", "flashed": False,
                          "application_health_verified": False}
                self.record.event("diagnosed", **result)
                return result
            self.phase = "backup"
            print("Reading 512 KiB backup (up to five minutes)...", flush=True)
            before = self._read_flash(FLASH_SIZE)
            self.record.snapshot("flash-before.bin", before)
            if sha256(before) != BASELINE_SHA256:
                raise ProbeError("flash differs from the earlier verified laptop backup; stop")
            self.phase = "backed-up"
            print("Backup saved and verified: {}".format(self.record.path), flush=True)
            print("Target PID 0x0467, 512 KiB, OPTR 0xFEFFFEAA; address 0x08000000")
            print("ERASE pages 0-9 ONLY; write {} bytes (+{} FF padding).".format(
                IMAGE_SIZE, PADDED_SIZE - IMAGE_SIZE))
            print("Image SHA-256: " + IMAGE_SHA256)
            print("Power loss can leave the application unbootable. No automatic rollback.")
            if confirm("Type exactly '{}' to erase/write: ".format(CONFIRMATION)).strip() != CONFIRMATION:
                self.phase = "cancelled"
                self.record.event("cancelled", flashed=False)
                return {"status": "cancelled", "flashed": False, "application_health_verified": False}
            self.phase = "erase-authorized"
            self._erase()
            for offset in range(0, len(self.padded), 256):
                self._write_block(offset, self.padded[offset:offset + 256])
            self.phase = "written"
            self.record.event("write-acknowledged", size=len(self.padded))
            print("Reading all flash for verification (up to five minutes)...", flush=True)
            after = self._read_flash(FLASH_SIZE)
            self.record.snapshot("flash-after.bin", after)
            expected = self.image + b"\xff" * (ERASE_SIZE - IMAGE_SIZE) + before[ERASE_SIZE:]
            if after != expected or sha256(after[:IMAGE_SIZE]) != IMAGE_SHA256:
                raise ProbeError("full readback mismatch; do not boot the application")
            self.phase = "verified"
            result = {"status": "flash_verified", "flashed": True,
                      "application_health_verified": False, "image_sha256": IMAGE_SHA256,
                      "flash_sha256": sha256(after), "outside_erased_pages_unchanged": True}
            self.record.event("verified", **result)
            return result
        except BaseException:
            failed_phase = self.phase
            self.phase = "failed"
            try:
                self.record.event("failed", phase=failed_phase,
                                  flash_may_be_modified=self.destructive_started)
            except OSError:
                pass  # Preserve original error; backup/intent already retained where possible.
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=Path("staged/firmware.bin"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--program", action="store_true", help="enable supervised hardware session")
    mode.add_argument("--diagnose", action="store_true", help="identify/read geometry only; erase/write blocked")
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--backup-root", type=Path, default=Path("rom-program-runs"))
    args = parser.parse_args(argv)
    programmer = None
    try:
        image = load_image(args.image)
        print("Approved image: {} bytes, SHA-256 {}".format(len(image), IMAGE_SHA256))
        if not (args.program or args.diagnose):
            print("Offline validation only. No serial import/open, backup or hardware operation.")
            return 0
        if not sys.stdin.isatty() or Path(args.port).resolve() != Path("/dev/ttyAMA0"):
            raise ProbeError("interactive TTY and PL011 /dev/ttyAMA0 required")
        import serial
        record = RunRecord(args.backup_root)
        print("Required setup (not measured): both boards powered; common GND;")
        print("approved UART wiring; CN4 removed. BEFORE READY, connect BOOT0 to target 3.3 V.")
        print("Full power cycle required after SWD debugging. Close Pi UART monitors/consoles.")
        print("Separate ST-LINK VCP may remain open receive-only; do not transmit.")
        if input("Type READY to open PL011, no bytes yet: ").strip() != "READY":
            record.event("cancelled-before-open")
            return 2
        try:
            with open_port(serial, args.port, 115200, 2.0) as port:
                port.reset_input_buffer()
                if input("Press/release B2 RESET, then type RESET: ").strip() != "RESET":
                    record.event("cancelled-before-sync")
                    return 2
                # Heartbeats can queue while the user is at the reset prompt.
                # ROM has not received sync yet, so no ROM reply is discarded.
                # Never clear input after sync: unexpected responses must fail.
                port.reset_input_buffer()
                programmer = Programmer(port, image, record, diagnostic_only=args.diagnose)
                result = programmer.run(input)
                print(json.dumps(result, indent=2))
                return 0 if result["status"] in ("flash_verified", "geometry_verified") else 2
        except serial.SerialException as exc:
            raise ProbeError(str(exc)) from exc
    except (ProbeError, OSError, termios.error, ImportError) as exc:
        print("ERROR: {}. Stopped; no retry.".format(exc), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled/interrupted; no retry.", file=sys.stderr)
        return 130
    finally:
        if args.program or args.diagnose:
            if programmer and programmer.destructive_started and programmer.phase != "verified":
                print("FLASH MAY BE INCOMPLETE. Keep BOOT0 HIGH; do not boot app. Plan recovery.")
            else:
                print("BOOT0/reset unchanged. Use powered-off BOOT0-to-GND return procedure.")
            print("Keep CN4 removed. Disconnect UART before powering either board off.")


if __name__ == "__main__":
    sys.exit(main())