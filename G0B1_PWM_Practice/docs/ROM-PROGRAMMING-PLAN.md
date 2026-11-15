# Supervised Pi → STM32 programming test

Prepared 2026-11-15 local. Programmer now deployed and software-tested on Pi;
no erase/write or hardware transition during preparation/deployment. Existing
read-ID helper stays strictly read-only.

## Pi deployment — 2026-11-14 21:23:48 UTC

**Resolved21:47:13 UTC:** new address diagnostics proved peripheral OPTR read
0x40022020 NACKs; size read works. RM0444 Tables16/17 pp82–83 supports stored
option word+complement at0x1FFF7800. Exact option-value and complement validation
now replace the unsupported peripheral read, without weakening value guards.
103 laptop/Pi tests pass. Deployed corrected code; diagnostic-only physical trial
passed512KiB andFEFFFEAA+valid complement. No backup/erase/write performed yet.
Latest code hashes: rom_program.py67a2a7b185b34093b446c4e688c397ae5b303924361a2529966e335720bc09c6;
test_rom_program.py16ae535ed70161a1aa2dd6787b6b2861288df48c6d1bfb23cafa09e85fa72f4b.

**Later hardware preflight 21:29:35 UTC failed safely:** ROM address NACK in
geometry reads, before backup/erase. No destructive prompt reached, no flash
commands sent. Exact rejected address not present in current diagnostic output.
Execution is blocked pending investigation; do not relax/bypass geometry guards.

User authorized next deployment step. Copied only rom_program.py,
test_rom_program.py and ROM-PROGRAM.md into /home/pi/aws-iot; all hashes match
laptop originals. 98 software tests passed in13.629s; staged image offline
validation, CLI help and dependency check passed. No dependencies changed.
Cloud source, both requirements files, read-ID helper, config/release and staged
firmware hashes match pre-deployment baseline. No hardware UART session, GPIO,
reset, flash, boot-config changes or MQTT client launch. Ready for the separately
confirmed supervised hardware trial; actual geometry reads remain unverified.

Deployed SHA-256:
- rom_program.py: bae1ad57a328c6344ec7b7928ba886f02127e0bbdc2bced1510ddf3b59f69313
- test_rom_program.py: 600ef1322345dbe208d9eef0125c3faacdbd1e387c3464ab8acfd18cdeb085c3
- ROM-PROGRAM.md: 5f1e43b66bbfbe57db805f13ed93f31d3de7270c4c2220551ea27545ae9b7e9c

## Software readiness — 2026-11-14 21:19:27 UTC

Added rom_program.py, test_rom_program.py and ROM-PROGRAM.md to existing laptop
gateway source (outside STM32 workspace). Default mode validates image offline;
--program requires READY, B2/RESET, geometry check, durable full backup matching
the pinned original hash, then exact destructive confirmation before erase.
Geometry reads require flash-size=512 KiB and full OPTR=0xFEFFFEAA; no fallback.
Full backup/readback and stage events use private, unique, fsynced run directories.

All **98 tests passed** locally: 43 programmer + 26 read-ID + 29 cloud regressions
in 5.775 seconds, including complete Linux PTY simulated programming. Actual
release image passed offline validation; CLI help, syntax and diagnostics passed.
Read-ID helper and cloud source/requirements hashes unchanged. No Pi deployment
or hardware operation during implementation. Next selectively deploy these three
files, run tests on Pi and offline image validation, then supervised hardware gate.
ROM geometry-register reads/page erase/write still untested physically; failure
must stop, not relax checks. AN3155 PDF still unavailable; packet formats checked
against stm32flash source, not claimed to have been verified from that PDF.

## Fixed target and image

- STM32G0B1RE, 512 KiB flash; observed ROM family PID 0x0467.
- Existing Pi staged image: /home/pi/aws-iot/staged/firmware.bin.
- Rechecked size: 19,212 bytes; SHA-256:
  e2c6d4bcf172cc803fa497a60bd0b625f5448a33433ab2d00bacec5fff6cc521.
- Reinstall the existing 0.1.0 image, not a new release.
- Start address 0x08000000; raw image ends at exclusive 0x08004B2C.
- Local HAL specifies 2 KiB flash pages and 8-byte double-word programming.
  Erase exactly pages 0–9: 0x08000000 through 0x08004FFF.
- Write 19,216 bytes: original image plus four 0xFF padding bytes, aligned to
  eight bytes. Do not modify the staged file. Remaining erased-page tail stays
  0xFF; do not needlessly program full pages of padding.
- Earlier verified OPTR 0xFEFFFEAA has nSWAP_BANK bit20 and DUAL_BANK bit21 set.
  RM0444 Rev6 pp72–73 confirms 2KiB pages0–9 entirely in lower-address bank1
  with nSWAP_BANK=1. The 512KiB device always operates dual-bank regardless of
  DUAL_BANK. Programmer requires the exact previously read options; never alters
  them. ROM page erase remains physically untested.

## Required programmer behaviour

1. Separate supervised programmer; preserve the read-ID helper's allowlist.
   No cloud-client integration, GPIO control, service installation or SDK changes.
2. Validate the complete image in memory against fixed size/hash/address and
   vector-table bounds before any destructive operation. Never re-open a mutable
   image midway through programming.
3. Manual BOOT0/B2 procedure as already tested, PL011 115200 8E1. Identify
   target and require advertised Read Memory 0x11, Write Memory 0x31 and
   Extended Erase 0x44. 0x21 is GO, not Write Memory; never send it in this test.
4. Before erase, read and save all 512 KiB of flash with a checksum. Compare
   against the earlier laptop backup; stop for investigation on unexpected
   changes. Confirm backup completion before offering the erase confirmation.
5. Explicit interactive destructive confirmation after showing image hash,
   target, address and exact page list. No mass erase, bank erase, unprotect,
   protection changes, option programming or automatic retry after failure.
6. Extended page erase only 0–9; verify those 20,480 bytes read back as 0xFF.
7. Write bounded blocks, at most 256 bytes, with addresses and lengths aligned
   to eight bytes. Last block is 16 bytes including the four padding bytes.
   Apply operation-specific bounded deadlines, checksums, ACK/NACK validation.
8. Mandatory full-flash readback: compare original 19,212 bytes byte-for-byte
   and by SHA-256, verify erased tail/padding, and require every byte outside
   the ten erased pages to match the pre-erase backup.
9. Close UART. User disconnects UART while both powered, powers target off,
   moves BOOT0 to target GND with CN4 still removed, then restores target power.
   Verify fresh application heartbeats via ST-LINK VCP. Heartbeats establish
   liveness, not complete PWM functionality or a new firmware version.

## Preparation gates before hardware execution

- Obtain/verify erase, write and read packet formats and G0-specific constraints.
  AN3155 PDF was not successfully retrieved previously; do not claim PDF review.
  Command identifiers and 256-byte transfer limit cross-checked against
  https://github.com/stm32duino/stm32flash/blob/main/stm32.c.
- Implement/test packet generation, exact page allowlist, alignment, boundaries,
  corrupt/truncated replies, NACK/timeouts, backup failures and readback mismatch
  with fake transports and PTYs; run existing 55 regressions as well.
- Review and selectively deploy only the separate programmer/tests/docs.
  No stm32flash executable was found on the Pi during preparation.
- No UART opening, BOOT0 changes or erase/write until preparation passes and
  the user explicitly confirms the destructive hardware step.

## Failure and recovery

This is an in-place reflash, not A/B OTA or automatic rollback. Power loss after
erase may leave no runnable application. Stop on failure, retain the backup and
log, leave BOOT0 selected for ROM, and plan a supervised recovery. Never launch
an unverified image. A recovery write needs its own deliberate confirmation.
The factory ROM remains the recovery entry; do not alter protection/options.

Current hardware stays in application mode: BOOT0 LOW, CN4 removed, Pi UART
disconnected. The staged AWS pipeline remains staging-only (`flashed=false`).