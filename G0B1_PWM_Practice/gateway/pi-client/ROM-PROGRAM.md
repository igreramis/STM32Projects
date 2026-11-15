# Fixed-image supervised ROM programmer

Updated 2026-11-14 21:48 UTC (Nov 15 local). Deployed and tested on Pi;
read-only hardware geometry verification passed. Full backup, erase/write and
full readback remain pending. Existing application was not modified.

## Geometry failure diagnosis and correction

Original peripheral FLASH_OPTR read at 0x40022020 was rejected by ROM with an
address NACK (21:43:21 UTC, diagnostic run rom-do3abidl). Flash-size read at
0x1FFF75E0 returned 0x200. This was an unsupported read target in this ROM,
not a reason to remove the option checks.

RM0444 Rev6 section3.4.1, Tables16/17 pp82–83 maps the corresponding stored
option word at 0x1FFF7800 and its bitwise-complement word at +4. The programmer
now reads those eight bytes, requires a valid complement and the SAME exact
expected option value0xFEFFFEAA. It never writes options. This is stored option
data rather than a direct peripheral register read; use the required fresh reset.

New mutually exclusive `--diagnose` mode runs only identification/geometry and
independently blocks erase/write commands, with no backup or destructive prompt.
Errors include address; logs capture geometry read start/value. Default remains
offline. At21:47:13 UTC, physical diagnosis passed:512KiB, stored optionsFEFFFEAA,
valid complement, exit0, UART closed. Run rom-engmxjyq. 103 tests pass locally
(5.814s) and Pi(13.961s). No flash/option programming has occurred.

## Scope

Separate rom_program.py imports only the transport/probe from rom_read_id.py;
the original read-only allowlist is unchanged. Cloud client/firmware modules,
requirements, credentials and staging pipeline are untouched. Uses the existing
optional pyserial==3.5 dependency; no new dependencies.

The default invocation is **offline image validation**. `--program` is an explicit
opt-in to an interactive hardware session; there is no unattended/yes bypass.
Defaults: image staged/firmware.bin; PL011 /dev/serial0; backup root
rom-program-runs relative to current directory. Use the existing gateway directory.
CLI `--help` describes options without importing/opening serial.

## Fixed policy (no user-supplied hashes, addresses or page ranges)

- Target family PID 0x0467, measured UART ROM version byte 0x31 (3.1).
- Require advertised Read Memory 0x11, Write Memory 0x31, Extended Erase 0x44.
- Require read-only flash-size register 0x1FFF75E0 = 512 KiB and stored option
  word at 0x1FFF7800 = 0xFEFFFEAA with valid complement. This pins RDP/options and non-swapped banks
  to the previously measured state. Any NACK/mismatch stops before erase.
- Image: 19,212 bytes, SHA-256
  e2c6d4bcf172cc803fa497a60bd0b625f5448a33433ab2d00bacec5fff6cc521.
  Validate vector-table SRAM/Thumb bounds; retain immutable bytes for whole run.
- Address 0x08000000; erase **only pages 0–9**, total 20,480 bytes.
- Write 19,216 bytes: 75 blocks of 256 and a final 16-byte block. Last four
  bytes are 0xFF padding for 8-byte alignment; do not change the staged file.
- Full-flash pre-erase backup must match earlier verified laptop SHA-256
  e46721beb7d9465aeb237c76f6dfa1bcfd584c4986578183bc7c66f582c9a5a5.
  An unexpected image is saved but no destructive confirmation is offered.

## Future supervised sequence — not authorization to run it now

1. Selectively deploy rom_program.py, test_rom_program.py and this guide into
   the existing Pi gateway. Keep the existing read-ID helper/dependency unchanged.
   Compare hashes, run all tests on Pi, and validate its real staged image offline.
2. Agree the hardware test. Leave ST-LINK serial terminals closed. Both boards
   powered before UART connection; common ground, manual BOOT0 target3V3,
   CN4 removed, GPIO17 disconnected, full target power cycle after SWD.
3. Interactive `--program` asks READY before UART open; clear stale input,
   then user presses/releases B2 and types RESET. Identify and read geometry.
4. Read all 512 KiB (up to five minutes), save private flash-before.bin,
   fsync and reopen/compare it, and record its checksum. Display backup path.
   Run directories are unique mode0700; files mode0600, never overwrite backups.
5. Display fixed target, image hash and pages. The destructive prompt requires
   `ERASE 0-9 WRITE ` followed by the full approved SHA-256. Any other input
   cancels without erase. Durable erase-intent is recorded before transmission.
6. Erase exact pages, verify every erased byte, write exact sequential image
   blocks, then read/save all flash again. Compare entire flash against expected
   image + erased tail + unchanged bytes beyond page9. Also verify image SHA-256.
7. Only then report flash_verified / flashed=true, with application health still
   false. This is standalone output, not a cloud acknowledgement/state change.
8. Close UART. Disconnect UART with both boards powered; target off; move BOOT0
   from target3V3 to targetGND; keep CN4 removed; target on. Verify fresh VCP
   heartbeat separately. Heartbeat is liveness, not full PWM validation.

Read transactions have a 2-second shared response deadline, erase 30 seconds,
write transactions 5 seconds, whole-flash reads 300 seconds. Each serial write
is bounded by 2 seconds. No blocking flush, retries, GO, mass/bank erase, RAM or
option writes, protection commands, GPIO, reset or SWD operations are implemented.
Serial exclusive locking is advisory; other programs must remain closed.

## Failure handling and limitations

Any exception stops the workflow and closes UART. After erase intent, an ACK
timeout can mean flash was modified; failure warns **do not boot the app**.
Keep BOOT0 HIGH and plan supervised recovery. No rollback or recovery bypass
exists; a partial/changed image will fail the pinned baseline on a new run.
Do not change that baseline to bypass a failure. Use a separately reviewed
recovery procedure. This is in-place programming, not A/B OTA.

Logs record stages, backup/readback hashes and failure phase; they are local
diagnostic records, not a durable cloud installation ledger. Disk/fsync failures
before erase block it; failures later stop the session. Full readback snapshots
remain available for mismatch investigation when their save succeeds.

## Protocol evidence and remaining hardware checks

- Packet framing cross-checked against stm32flash stm32.c: command/complement,
  four-byte big-endian address+XOR; Read Memory length/complement, ACK then data
  with no trailing ACK; Write Memory length-minus-one+data+XOR then ACK;
  Extended Erase 16-bit count-minus-one and page IDs, all big-endian, XOR then ACK.
  Source: https://github.com/stm32duino/stm32flash/blob/main/stm32.c.
  Its inspected device table did not list PID0467; it is a protocol cross-check,
  not proof of target support. AN3155 PDF has not been successfully retrieved.
- Local RM0444 Rev6 pp72–73 bank/page layout: 2KiB page0 at 0x08000000,
  bank1 at lower address when nSWAP_BANK=1. The 512KiB device always operates
  dual-bank regardless of DUAL_BANK. Local HAL requires double-word writes.
- AN2606 Rev61 chapter44 pp220–224 confirms USART1 8E1; V9.2 in its table is
  kept separate from actually observed UART version byte0x31.
- ROM reads of size and stored option bytes now pass physically. Full-flash
  backup/page erase/write/readback remain untested. No checks are relaxed if
  reads fail. Emulator tests do not validate silicon behavior.

## Software validation

Laptop Python3.8: **98 tests passed** in5.775s: 43 programmer +26 read-ID +29
cloud regressions. Independent emulator decodes packets and enforces exact page
and write ranges, validates checksums and simulates complete flash contents.
Tests cover invalid images/vectors, forbidden commands, geometry/version/PID
mismatches, backup/disk/fsync failures, cancellation, NACK/noise/truncation/timeouts,
no retry, erased-byte mismatch, image/outside-region corruption, CLI close paths
and a full real pyserial Linux PTY backup/erase/write/readback simulation.
PTYs use8N1 only; production uses8E1. Physical parity is not tested by PTYs.

The real release image passed offline validation. CLI help passed, Python syntax
checks passed, editor diagnostics clear. Initial test fixture was one byte too
long; corrected its synthetic-tail padding; no production protocol fix needed.