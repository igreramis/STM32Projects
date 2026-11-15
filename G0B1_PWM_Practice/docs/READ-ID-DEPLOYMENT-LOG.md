# Read-ID helper deployment — 2026-11-15 local

Scope approved: deploy the helper and its dependency to the existing Pi gateway,
then run software-only tests. No hardware UART session, GPIO changes, STM32
reset/programming, MQTT client start, or boot configuration changes.

## Actions

1. Reviewed the tested helper and deployment instructions on the laptop.
2. Selected four files only: rom_read_id.py,
   test_rom_read_id.py, requirements-hardware.txt and ROM-READ-ID.md.

All times below are UTC on 2026-11-14 (2026-11-15 local).

3. **19:54:44:** Connected using SSH with existing host-key verification.
    Pi uptime approximately 15 minutes; Wi-Fi connected. Existing gateway and
    virtual environment present; none of the four helper files existed.
    Recorded baseline hashes of cloud source, configuration and staged firmware.
4. **By 19:55:55:** Copied only the four files into /home/pi/aws-iot.
    All four remote SHA-256 hashes matched laptop originals. Installed only
    pyserial 3.5 into the existing Python 3.13.5 virtual environment, with no
    dependency upgrades. AWS SDK 1.31.0 and AWS CRT 0.36.1 unchanged.
5. **19:55:55:** All **55 software tests passed** in 1.662 seconds:
    26 helper tests and 29 cloud regressions. Helper CLI help succeeded;
    dependency check found no broken requirements. Tests used mocks and Linux
    pseudo-terminals, not the physical UART; physical parity remains untested.
6. **19:56:14:** Cloud source, main requirements, configuration, release metadata
    and staged firmware hashes matched the baseline. Wi-Fi remained connected;
    read-only pin inspection showed GPIO14/15 on PL011 and GPIO17 an input.
7. **Final process check:** Exact Python script-argument inspection found zero
    gateway clients and zero probes. Earlier broad pgrep counts matched the SSH
    command itself, not running applications; no client was started or stopped.

**Result: deployment complete.** No hardware UART opened, GPIO changed, STM32
reset/programming performed, or boot configuration changed. No actual ROM
handshake yet. Next step is a supervised hardware read-ID test after the user
returns and confirms power/UART connections.

## Subsequent supervised hardware test — recorded 20:09:55 UTC

- User confirmed both boards powered and agreed UART/GND/BOOT0/CN4 wiring.
- Started interactive helper; user confirmed READY, then pressed/released B2
    and confirmed RESET. Physical UART exchange at 115200 8E1 succeeded.
- Result: rom_identified; family device ID 0x0467; UART bootloader version
    raw 0x31 (3.1); raw status bytes [0, 0]. Advertised commands:
    00, 01, 02, 11, 21, 31, 44, 63, 73, 82, 92.
- Only sync/Get/Get Version/Get ID sent. No erase, write, GO or protection
    commands. Helper exited 0 and closed UART; SSH session ended.
- BOOT0 remains at target 3.3 V, CN4 removed. Return to application and
    application-health check remain pending. This was identification, not flashing.

## Return to application — 20:12:55 UTC

- User confirmed safe UART-disconnect/power-off transition, BOOT0 moved to
    target GND, CN4 left removed, then target powered on again.
- Read ST-LINK VCP at 115200 8N1, clearing old input first. Received two fresh
    `Hello from NUCLEO-G0B1RE!\r\n` messages. Capture exited 0; port closed.
- Application main-loop/UART liveness verified; PWM/full functionality not
    tested. No transmitted serial data, SWD attach, software reset or flash action.
- Current state: BOOT0 LOW, CN4 removed, Pi UART disconnected. No flashing
    authorized or performed; read-ID and return-to-application checks complete.

## Programmer preparation — 21:19:27 UTC

- Added a separate fixed-image programmer, tests and guide to existing laptop
    gateway source. Existing read-ID helper and cloud files unchanged by hash.
- Default mode is offline; hardware mode requires geometry validation, durable
    full backup matching pinned baseline, and explicit confirmation before erase.
- 98 local software tests passed (43 new +55 existing), including full PTY
    simulation and failure paths. Real firmware image offline validation passed.
- No Pi deployment or hardware operation during this preparation. Next:
    selective programmer deployment and Pi software tests, then supervised trial.

## Programmer deployment — 21:23:10–21:23:48 UTC

- Connected with strict SSH host-key verification. None of the three target
    programmer files existed; copied only programmer, tests and guide to existing
    /home/pi/aws-iot. All three SHA-256 hashes match laptop originals.
- All **98 Pi software tests passed** in13.629s. Staged firmware passed offline
    validation. CLI help and dependency check passed; no dependency installation.
- Before/after hashes unchanged for cloud source, both requirements files,
    read-ID helper, config, release metadata and staged firmware.
- No hardware UART opened, GPIO/reset/flash/configuration action, or MQTT client
    start. Next is the separately confirmed supervised hardware trial.

## Hardware preflight stopped — 21:29:35 UTC

- User confirmed ROM-mode wiring and READY, pressed/released B2, confirmed RESET.
- Programmer stopped with `address: ROM returned NACK` during identifying /
    geometry checks, before backup or erase confirmation. Exit1; UART closed.
- Event log in /home/pi/aws-iot/rom-program-runs/rom-jhwd0y61 confirms
    flash_may_be_modified=false. No erase/write command sent, no automatic retry.
- Current diagnostics do not identify which geometry address was rejected;
    investigate before changing protocol/guards. No guard bypass authorized.
- BOOT0 remains HIGH; CN4 removed, Pi UART connected, both boards powered.

## Geometry diagnosis/fix — 21:43–21:48 UTC

- Added address-specific logs and read-only --diagnose mode with independent
    erase/write blocking.102 tests passed locally/Pi; deployed two changed files.
- Supervised diagnosis proved512KiB size read works; peripheral OPTR address
    0x40022020 NACKs. No destructive operation reached.
- Checked RM0444 Rev6 pp82–83: stored options at0x1FFF7800 with complement+4.
    Changed the read to this documented location, required valid complement and
    unchanged exactFEFFFEAA value. No option writes or guard bypass.
- 103 tests passed locally5.814s/Pi13.961s; matching code hashes on deployment.
- User-confirmed READY/B2 RESET; corrected read-only trial passed21:47:13 UTC.
    Run rom-engmxjyq, geometry_verified, flashed=false, exit0 and UART closed.
- Wiring unchanged. Full flash backup, erase and programming remain pending.