# Local version trials — November 15, 2026

## Artifacts and tools

- Three Debug images built with identical source and flags, differing only in
  APP_VERSION: 0.1.0, 0.2.0, 0.3.0. Each is 19536 bytes (no write padding).
- Laptop releases: ~/Downloads/resumes/plasmaleap/aws/releases/stm32g0b1/
  with one folder per version. Original unversioned 0.1.0 preserved in
  archive/0.1.0-original-unversioned. These local replacements are NOT the
  previously published cloud release; AWS objects and staging remain unchanged.
- CMake targets firmware_0_1_0, firmware_0_2_0, firmware_0_3_0 generate bin/elf/map
  under build/Debug/versions. tools/package_local_versions.py validates and packages.
- Application heartbeat every 10 seconds:
  `Hello from NUCLEO-G0B1RE! firmware=<version>`.
- USART2/ST-LINK output retained; TX-only USART1 mirrors to PA9/D8 for Pi RX.
  AF1 verified in installed CubeMX GPIO-STM32G0Bx_gpio_v1_0_Modes.xml.
  PA10 remains input in app. App 115200 8N1, ROM 115200 8E1.
- Helpers in existing external pi-client source: rom_local.py and
  heartbeat_monitor.py with corresponding tests. rom_program.py only changed
  its padding banner to use actual padding; original default pins remain.
- rom_local validates all three images, vector tables, original baseline and
  computed full-flash hashes. --from-version explicitly selects known expected
  predecessor (original/0.1.0/0.2.0/0.3.0); backup must match before erase gate.
  No dynamic acceptance of arbitrary flash, cloud, GPIO, GO, or retry.
- Deployed to /home/pi/aws-iot: helpers/tests, local-releases/<version>/,
  local-releases.json, local-baseline.bin (mode0600).
- 161 tests PASS locally (7.086s), Pi (19.637s). All three offline validations
  PASS on Pi, matching image hashes. Cloud client/stager/read-ID/staged image
  unchanged. Initial deployment attempt stopped on network timeout before copy;
  user restored prior network and deployment then succeeded.

| Version | Image SHA-256 | Expected full flash SHA-256 |
|---|---|---|
| 0.1.0 | 5d12c7bd78467bf2d050c92a08326588423f7b1a6f3d7008983d6a839c30318b | c7fbe204b55db318dd8534b26a7e6dcfe1f1dc007611582b6b392778edb35170 |
| 0.2.0 | 86dcf2df3f328bfb074bddd3fcf661b3f964e656d0f2755f684960d7a9a3f6fd | 5fd3f36a1f94f07e42709ba1e4976a080caea92425d4730314a2866d80fd718e |
| 0.3.0 | f5f2f9c3c24f9f2f3e9931ad78446938454dd303bea4d866ae1b076dfb81ebbb | d484075d8aee2d858fa3c0d55cf2746fd2c3adfead11654c6a8f9711172bfb87 |

## Current physical trial

**Latest — 0.3.0 sync issue:** 0.2.0 Pi heartbeat verified twice at Nov15
01:11:16.062975/01:11:26.071278 UTC after powered BOOT0 LOW + B2 reset;
USB/UART disconnection was not required with both boards powered and no SWD.
User's subsequent manual 0.3.0 attempt stopped at sync with0x48, before erase.
That byte is consistent with a heartbeat, but does not prove boot selection
failed: input was cleared before the reset prompt, allowing stale app bytes
to queue while waiting. New regression reproduced exactly0x48 from that window.
Fix: clear input again AFTER RESET confirmation and BEFORE first sync byte;
never flush after sync or ignore unexpected replies. Also clarified readiness
text as required setup, not measured BOOT0 state, and separate VCP receive-only.
163 tests passed laptop7.352s/Pi19.596s; two-file fix deployed with matching
hashes. No UART or flash operation during fix. Restart programmer to load fix.
0.3.0 hardware success remains pending; current BOOT0 level not reconfirmed.

**Latest:** 0.1.0 application verified directly on Pi after user-confirmed safe
return/reconnection. Two fresh matching `firmware=0.1.0` lines at
2026-11-15 00:41:02.697273 and 00:41:12.705599 UTC; monitor exited successfully
and released UART. This proves version/main-loop/UART liveness, not PWM health.
User then confirmed ROM wiring for 0.2.0 and fresh B2 RESET.
Active session 12a0443c-d2b5-4d1a-b01a-7fa601d3e86d targets 0.2.0 from known
0.1.0. Geometry passed; full512KiB backup saved/verified in run rom-5_jazc7v.
User explicitly authorized 0.2.0 erase/write after backup and power-loss warning;
exact hash phrase submitted. Erase/write and full readback PASSED, exit0;
UART closed and SSH ended. Full flash SHA-256
5fd3f36a1f94f07e42709ba1e4976a080caea92425d4730314a2866d80fd718e matches
expected 0.2.0; outside_erased_pages_unchanged=true. No active programmer.
0.2.0 application heartbeat remains pending; safe return to app required.
Keep BOOT0 HIGH, CN4 removed, UART/common GND connected and both boards powered.

0.1.0 from original: user confirmed safe powered-off BOOT0 change and wiring:
BOOT0 HIGH to target3.3V, CN4 removed, both powered, common ground,
Pi TX physical8 -> PA10/D2, Pi RX physical10 <- PA9/D8; GPIO17 disconnected.
READY and fresh B2 RESET confirmed. Geometry reads passed512KiB/FEFFFEAA.
Full512KiB backup saved and verified in run rom-p711vhho. User explicitly
authorized 0.1.0 erase/write (pages0–9,19536bytes) after backup and power-loss
warning. Exact hash confirmation submitted. Erase/write and full readback PASSED:
flash_verified, flashed=true, outside_erased_pages_unchanged=true. Full hash
c7fbe204b55db318dd8534b26a7e6dcfe1f1dc007611582b6b392778edb35170 matches
the expected 0.1.0 flash. Exit0; UART closed and SSH session ended.
Application heartbeat is NOT yet verified. Last wiring remains ROM mode.
Next supervised return to app: disconnect UART TX/RX while both powered,
target off, BOOT0 to target GND, CN4 left removed, target power restored.
Reconnect crossed UART only with both powered; then receive two matching
0.1.0 heartbeats on Pi. Do not start 0.2.0 until that check succeeds.
Repeat with explicit predecessors 0.1.0->0.2.0->0.3.0 after each success.

## Console use (on Pi from /home/pi/aws-iot)

Run `.venv/bin/python heartbeat_monitor.py` for timestamped continuous output.
Ctrl+C releases the UART. Stop it BEFORE running rom_local.py; both require
exclusive access. It must not consume ROM replies. Heartbeats pause in ROM.
Bounded check: `.venv/bin/python heartbeat_monitor.py --version 0.1.0 --count 2 --timeout 30`.
Restart after each supervised return to application; terminal scrollback retains
earlier version lines. Keep UART disconnected when either board is unpowered;
reconnect only after both boards are powered. No automated boot/reset circuit yet.