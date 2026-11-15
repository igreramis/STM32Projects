# AWS / Pi / STM32 OTA — shared handover

Last updated: cloud-triggered installer implementation completed locally (not deployed).
Purpose: shared facts and change record for separate cloud and hardware chats.
Chats do not automatically exchange context. Read this document AND current code
before working; update it after each milestone. Do not rely on another chat's
session memory being available.

## Current milestone and scope

**Source-control update:** All gateway Python source/tests, dependency pins,
safe configuration/service examples and original non-secret release approval
are now included under [gateway/pi-client](../gateway/pi-client/README.md).
309 tests PASS from this repository copy in 14.306s. Make subsequent tracked
gateway edits here rather than only in the old external development directory;
there is no automatic synchronization. Private config, keys, signed URLs,
virtual environments, firmware artifacts, backups and runtime state are excluded.
This does not deploy or start anything on the Pi.

**CURRENT — local cloud-install implementation:** See
[CLOUD-INSTALL.md](CLOUD-INSTALL.md) for activation prerequisites, command fields,
operator workflow, evidence flags and recovery limits. Gateway source outside
this workspace now includes ota_installer.py, ota_jobs.py and three new test
modules; client.py has explicit `--ota-config` integration and rom_read_id.py
has an extracted sync method. Configuration/service examples are templates only.
`stage_firmware` remains download-only. Approved install jobs use a bounded
120-second ROM wait, automatic backup/erase/program/full readback, manual
BOOT0/B2 prompts and two fresh expected-version application heartbeats.
Persistent dedup, single worker, pre-erase durable intent and PUBACK-tracked
results survive restart without rerunning unfinished jobs. PL011-only guard
applies before UART open. Regression fixed: redelivery of an already-PUBACKed
final command now replies again without reinstalling.

309 local software tests PASS in 15.345s; no live cloud/hardware test, deployment,
MQTT client start, service enablement, S3 upload, GPIO or option-byte operation
performed in this task. Syntax check passed. Editor reports unresolved imports
in the external installer under the STM32 workspace; runtime suite resolves
these using the gateway directory and existing test dependency directory.
No claim of a working live OTA installation yet.

**Current hardware evidence:** [LOCAL-VERSION-TRIALS.md](LOCAL-VERSION-TRIALS.md)
records successful local 0.1.0 and 0.2.0 flash/readback plus Pi heartbeat checks.
User subsequently supplied successful local 0.3.0 flash_verified output with
full hash d484075d8aee2d858fa3c0d55cf2746fd2c3adfead11654c6a8f9711172bfb87;
0.3.0 application heartbeat has not been confirmed. Current physical BOOT0/power
state is not freshly verified. No programming session is active from this task.

The entries below are historical and can contain superseded implementation,
wiring and next-step statements. Use the current section and linked guides first.

**LATEST — programming/readback PASSED:** run
/home/pi/aws-iot/rom-program-runs/rom-9ct2dwzl saved and verified the full
512 KiB backup against the pinned original hash. User explicitly selected
"Authorize erase and program" after backup verification and the power-loss
warning. Exact approved image hash confirmation was submitted. Erase pages
0–9, erased-byte verification, write and full 512 KiB readback completed.
Result flash_verified, flashed=true, outside_erased_pages_unchanged=true;
exit0, UART closed and SSH ended. Image SHA-256:
e2c6d4bcf172cc803fa497a60bd0b625f5448a33433ab2d00bacec5fff6cc521.
Full readback SHA-256:
e46721beb7d9465aeb237c76f6dfa1bcfd584c4986578183bc7c66f582c9a5a5
(same as original: this reinstalls the same image, not a version upgrade).
Programmer result application_health_verified=false remains an accurate record
of its ROM-only session. Subsequent separate heartbeat check PASSED:
user confirmed UART TX/RX disconnected while both powered, target powered off,
BOOT0 moved to target GND, CN4 left removed and target USB power restored.
After clearing stale input, receive-only ST-LINK VCP at115200 8N1 captured
"Hello from NUCLEO-G0B1RE!" at21:59:12.743782 and21:59:22.751148 UTC.
Check exited0 and VCP closed; no TX/SWD. This proves application main-loop/UART
liveness, not full PWM/functional health. Current state: application running,
BOOT0 GND, CN4 removed, Pi UART TX/RX disconnected, target USB powered.
No option writes, automatic retry, rollback or cloud install integration.

**LATEST — geometry diagnosis resolved:** diagnostic-only mode proved size
0x1FFF75E0 reads512KiB, but peripheral FLASH_OPTR0x40022020 returns address NACK.
RM0444 Rev6 Tables16/17 pp82–83 documents stored option word0x1FFF7800 plus its
complement at+4. Replaced the unsupported peripheral read with that eight-byte
read, validated complement AND retained exactFEFFFEAA check (no bypass/writes).
103 tests pass laptop/Pi; diagnostic-only hardware trial passed21:47:13 UTC,
run /home/pi/aws-iot/rom-program-runs/rom-engmxjyq. UART closed, exit0,
flashed=false. Current wiring unchanged: BOOT0 HIGH,CN4 removed,UART connected,
both powered. No full backup/erase/write yet. Next resume supervised programming
preflight and full backup; explicit erase confirmation remains mandatory.

**Latest hardware attempt — STOPPED before backup/erase:** user confirmed
manual BOOT0 HIGH, CN4 removed, both boards powered, common ground and crossed
Pi UART connected; READY then user-confirmed B2/RESET. At 21:29:35 UTC,
programmer exited1: `address: ROM returned NACK`. Call order puts this in
geometry reads after successful ROM identity/capability validation. Existing
error text does not distinguish flash-size address 0x1FFF75E0 from OPTR
0x40022020; do not claim which one was rejected without further evidence.
Run /home/pi/aws-iot/rom-program-runs/rom-jhwd0y61/events.jsonl records
phase=identifying, flash_may_be_modified=false. No backup, erase prompt,
erase/write command, or retry. UART closed. Current reported hardware remains
BOOT0 HIGH, CN4 removed, Pi UART connected, both powered. Do not perform the
next erase step or bypass geometry guards. Next investigate permitted ROM
read ranges and improve address-specific diagnostics before a supervised retry.

**Programmer deployed/tested on Pi:** see [ROM-PROGRAMMING-PLAN.md](ROM-PROGRAMMING-PLAN.md).
Added rom_program.py, test_rom_program.py, ROM-PROGRAM.md to existing laptop
gateway source. 98 tests passed (43 programmer +55 existing); real release image
passed offline validation. Default offline, exact geometry/image/baseline guards,
durable backup, explicit pre-erase confirmation, pages0–9 only and full readback.
Read-ID helper and cloud files unchanged. Three files deployed to existing
/home/pi/aws-iot with matching hashes; 98 Pi tests passed in13.629s, real staged
image offline validation/help/pip check passed. No dependencies installed or
changed. No physical UART opened or hardware programming performed during this
deployment. Next supervised hardware confirmation; geometry reads must succeed
and full backup must match before explicit erase prompt. Keep current
application-mode wiring until supervised instructions.

**Verified:** AWS MQTT command -> private versioned S3 download -> verified
firmware staged on the Raspberry Pi -> result acknowledgement accepted by AWS.

**Not implemented:** Pi -> STM32 ROM bootloader programming, GPIO boot/reset
control, post-flash application health verification, or cloud-triggered flashing.

The user wants to PLAN hardware integration in a separate chat first. Preserve
the cloud setup. Do not erase/program the board, alter option bytes, toggle
boot/reset pins or change Pi UART/boot configuration until the plan and required
hardware actions have been explicitly agreed. Do not equate permission to plan
with permission to flash. Network-failure testing is deferred by user choice.

## Resume here — ordered next steps (2026-11-14 pause)

**Latest return-to-application milestone:** user confirmed UART disconnected
while both boards powered, target powered off, BOOT0 moved from target 3.3 V
to target GND, CN4 left removed, then target USB power restored. At 20:12:55 UTC,
read-only ST-LINK VCP capture at 115200 8N1 received two fresh
`Hello from NUCLEO-G0B1RE!\r\n` messages after clearing buffered input.
Port closed; no serial TX, SWD attach, software reset or flash operation.
This verifies application main-loop/UART liveness, not PWM behaviour or full
functional health. Current reported wiring: BOOT0 LOW, CN4 removed, Pi UART
disconnected, common ground retained. Read-ID and return-to-app checks complete;
any erase/program/readback test still needs a separate plan and authorization.

**Latest hardware milestone — supersedes pre-test states below:** user confirmed
both boards powered, common ground, CN4 removed, BOOT0 at target 3.3 V,
Pi pin 8 TX -> PA10/CN9 pin 3 and Pi pin 10 RX <- PA9/CN5 pin 1;
GPIO17 disconnected. After READY and user-confirmed B2 reset, the physical
PL011/USART1 exchange at 115200 8E1 succeeded. Helper exited 0 and closed UART.
PID **0x0467** (family ID); Get/Get Version byte **0x31 (3.1)**; raw status
bytes [0, 0]. Advertised commands: 00, 01, 02, 11, 21, 31, 44, 63, 73, 82, 92.
Only sync/Get/Get Version/Get ID sent; no erase/write/GO/protection commands.
The actual UART version byte is recorded separately from the AN2606 V9.2
documentation reference; do not replace the measured value with that reference.
Application health is not yet verified. BOOT0 remains HIGH; no return-to-app
transition performed. Next: disconnect UART while both powered, power target
off, move BOOT0 from target 3.3 V to target GND, keep CN4 removed, power target
on and verify the existing application's observable behaviour.

**Latest plan revision:** user has no resistors; supervised manual BOOT0 jumper
selection and B2 reset replace Pi GPIO17 control for the initial test. Pi will
provide UART only. Never connect a rail jumper to PA14 while CN4 connects
ST-LINK SWCLK, or while another device drives PA14. Final jumper/power procedure
must be agreed before wiring; nBOOT_SEL change is now approved and complete. Earlier GPIO17
instructions below are deferred, not instructions for this manual test.

**Original initial approach (GPIO17 now deferred):** Pi controls BOOT0 and USART1; the user presses B2
(the physical RESET button). No Pi-to-NRST wire for this first test. Automated
reset is deferred, not a prerequisite for the manual test. No custom STM32
bootloader is required.

**Current state:** ROM USART1 support confirmed in AN2606 Rev 61, Table 95,
page 220. PA9 TX = CN5 pin 1 / D8; PA10 RX = CN9 pin 3 / D2; 8E1 framing.
BOOT0 = PA14 / CN7 pin 7. SB16/SB18 stay unchanged; ST-LINK VCP remains on
PA2/PA3. CN4 was fitted at the last inspection. Pi serial configuration was
approved, applied and verified after reboot: PL011 on GPIO14/15, Bluetooth off,
serial console removed, Wi-Fi/SSH working. No BOOT0 drive, wiring or ROM
handshake performed. Approved nBOOT_SEL-only programming and option reload/reset
completed: FLASH_OPTR=0xFEFFFEAA, FLASH_SECR=0; nBOOT_SEL=0 selects the physical
BOOT0 pin. All 512 KiB of application flash compared byte-identical before/after.
Latest user-reported physical state: Pi powered on, Nucleo still off, CN4 caps removed, manual
BOOT0 to target 3.3 V, common ground connected, TX/RX and GPIO17 disconnected.
Photo suggested Nucleo ground lead was on D12; user reports correction to GND
and requested no further photo review. Do not claim independent photo verification.
Nucleo uses laptop USB; Pi uses its own adapter. Do not connect their power rails.
Read-ID helper now deployed to the existing Pi gateway; all 55 software-only
tests passed there. No physical UART opened and no ROM handshake performed.

Perform the following in order; this checklist is NOT authorization to execute
hardware/configuration changes:

1. **Re-establish physical/runtime state.** Confirm board/power connections,
   CN4 position, Pi wiring and any load on PWM PB14. Recheck running processes
   before accessing ST-LINK or starting a gateway client. Do not open/dump the
   user's scratch notes or signed URLs. No new MQTT client is needed here.
2. **Pi UART/GPIO inspection and approved UART setup — completed 10:12 UTC.**
   /dev/serial0 -> ttyAMA0 (PL011), GPIO14 TX / GPIO15 RX (physical 8/10).
   Bluetooth disabled; UART console removed; Wi-Fi and SSH verified after reboot.
   User pi has read/write permissions. No serial data sent or 8E1 session opened;
   framing will be set by the future helper. GPIO17 remains an unclaimed input
   and BOOT0 candidate, not yet wired/driven. Continue with the wiring plan.
3. **Finalize wiring and power plan.** Pi TX -> PA10, Pi RX <- PA9, common
   ground; NO Pi BOOT0 or reset connection for the manual test. Specify manual
   PA14 selection to target 3.3 V or GND, never both, with no conflicting driver.
   Confirm target application does not drive PA14 before using a rail jumper.
   Confirm header orientation and
   avoid back-powering through UART/BOOT0. CN4 must isolate SWD before Pi drives
   PA14; Pi BOOT0 drive must be disconnected/isolated when CN4 is fitted for
   debugging. Account for ROM configuring PB14 as SPI2 MISO, not PWM.
4. **Read-ID helper deployed and tested on Pi — completed 19:55 UTC.** Existing gateway source now
   contains rom_read_id.py, test_rom_read_id.py, requirements-hardware.txt and
   ROM-READ-ID.md. 26 helper + 29 existing cloud tests passed. Only sync/Get/
   Get Version/Get ID, 8E1, deadlines, manual B2 prompts, no GPIO or flash commands.
   AN3155 download failed; framing cross-checked against stm32flash source;
   no AN3155 page citations claimed. Four files selectively deployed with matching
   hashes; pyserial 3.5 installed in existing Pi Python 3.13.5 venv. All 55 tests
   passed on Pi; no hardware execution yet. Next supervised power/UART/B2 test.
5. **One-time option change — completed 10:39 UTC, do not repeat.** User
   approved nBOOT_SEL 1 -> 0 and reload/reset. Post-reload OPTR=0xFEFFFEAA,
   SECR=0. nBOOT1=1, nBOOT0=1, NRST_MODE=3, RDP=AA and BOOT_LOCK=0 preserved.
   Full flash before/after comparison passed. No application programming.
6. **Approved physical transition.** Disconnect all power sources before
   changing jumpers/wires; remove both CN4 caps, attach agreed Pi wiring, and
   restore power in the agreed safe order. A full target power cycle after SWD
   access is needed to clear the documented debugger BOOT0 latch; pressing B2
   alone is not a substitute. Do not reconnect a debugger during ROM testing.
   Leave ST-LINK serial console idle (no TX traffic), especially during ROM
   interface selection; keeping CN2 for power/VCP does not require CN4 fitted.
7. **Read-ID-only ROM test.** Manual jumper selects BOOT0 HIGH; prompt user to press/release
   B2. With BOOT0 stable at reset release, send 0x7F and verify ACK, ROM version,
   commands and device ID. Record actual results. No firmware erase/write.
8. **Return to the existing application.** Manual jumper selects BOOT0 LOW using the agreed safe transition; prompt another
   B2 press/release. Check the existing application's observable behaviour;
   merely reading an ACK or releasing reset is not proof the application runs.
   The application serial console is available only when its firmware runs,
   not while ROM runs. nBOOT_SEL stays 0; do not toggle it for each boot mode.
9. **Only after that succeeds:** plan and separately authorize one local
   staged-image erase/program/readback test. Revalidate exact bytes/target and
   exclusive ownership. Cloud-triggered install and automatic Pi reset come
   later; stage_firmware remains staging-only.

**Latest process status:** final OpenOCD verification resumed the CPU and exited
0 at 10:39:06 UTC. Pi SSH/Wi-Fi verified again at 19:56 UTC; exact Python script
argument inspection found zero client.py and zero rom_read_id.py processes.
Earlier broad pgrep counts matched the SSH command itself. No client started.
Recheck before any future client launch.

## Deployment milestone — 2026-11-14 19:54–19:56 UTC

User authorized unattended selective deployment and software-only testing after
powering Pi on. Copied the four helper files into /home/pi/aws-iot; all hashes
matched laptop source. Added only pyserial==3.5 to the existing venv using the
separate hardware requirements, with no dependency upgrades. Actual runtime
Python 3.13.5; AWS SDK 1.31.0 and AWS CRT 0.36.1 unchanged.

55 tests passed in 1.662 seconds (26 helper + 29 cloud); CLI help and dependency
check passed. PTY tests do not validate physical 8E1. Before/after SHA-256 values
matched for client.py, firmware.py, requirements.txt, config.json, release.json
and staged/firmware.bin. No credentials copied, MQTT client launched, physical
UART opened, GPIO changed, STM32 reset/flashed, or boot configuration edited.
Simple action log: [READ-ID-DEPLOYMENT-LOG.md](READ-ID-DEPLOYMENT-LOG.md).

## Verified evidence

| Check | Result |
| --- | --- |
| Pi telemetry in AWS console | Screenshot verified, device pi-lab-01 |
| get_status console round trip | test-001, status ok, screenshot verified |
| Python tests on Pi | 55 passed: 26 helper + 11 client + 18 firmware tests, 19:55 UTC |
| S3 download command | stage-001, status verified at 2026-11-14 05:06:02 UTC |
| MQTT result | PUBACK for verified response; console receipt of this firmware response not yet screenshot-confirmed |
| Independent staged file check | 19,212 bytes, SHA-256 matches original; permissions 600 |
| Latest bounded client run | 05:05:42–05:15:43 UTC; 60 telemetry PUBACKs; clean exit 0 |
| Process check | No client process found at 05:19:19 UTC; recheck before starting one |

No STM32 source changes were made during cloud setup. Existing local Debug build
was used. The 0.1.0 version is a package label, not newly embedded firmware data.

## Where the software actually lives

Laptop workspace (STM32 C/CMake):
`/home/wh/STM32Projects/G0B1_PWM_Practice`

Laptop gateway source (OUTSIDE that workspace):
`/home/wh/Downloads/resumes/plasmaleap/aws/pi-client`

| Gateway file | Role |
| --- | --- |
| client.py | MQTT connection, telemetry, command dispatch, bounded replay cache |
| firmware.py | Release approval validation and HTTPS download/verification only |
| config.json | MQTT endpoint/client ID/certificate paths |
| release.json | Locally approved target/version/size/hash/S3 version |
| test_client.py / test_firmware.py | Existing unit tests; preserve and extend |
| requirements.txt | awsiotsdk==1.31.0, awscrt==0.36.1 |
| stage-command.template.json | Non-secret stage command template |
| README.md | Run/test instructions and implementation limitations |

Laptop upload artifacts:
`/home/wh/Downloads/resumes/plasmaleap/aws/releases/stm32g0b1/0.1.0/`
contains firmware.bin, manifest.json, README.md.

Pi deployment: `/home/pi/aws-iot`
- Interpreter: `/home/pi/aws-iot/.venv/bin/python` (Python 3.13.5).
- Staged image: `/home/pi/aws-iot/staged/firmware.bin`.
- Approved metadata: `/home/pi/aws-iot/release.json`.
- Old client backup: `/home/pi/aws-iot/pre-staging-client-20261114.tar.gz`
  (source/config/docs only, no certificate keys).
- Candidate test copy: `/home/pi/aws-iot/candidate-stage-20261114` (not canonical).
- No systemd service or automatic application startup installed.

Edit laptop source first, test, deploy selected files and record the deployment.
Do not leave undocumented Pi-only edits or create a competing gateway project.
If a different workspace/window cannot access the source, open/add the existing
directory rather than assuming these are synchronized copies.

## Pi access and runtime

- Raspberry Pi 3 Model B Rev 1.2; Pi OS Lite arm64, Debian 13 Trixie.
- SSH alias `pi`: user pi, last IP 192.168.1.194, key-based login verified.
- Use strict host checking; unexpected host-key changes require verification.
- Wi-Fi 2.4 GHz, SSH after reboot and synchronized time verified.
- Local laptop Python 3.9 is NOT the Pi's Python 3.13 interpreter.
- Sudo may require the user to type a password in the terminal. Never collect
  passwords, private keys or tokens through chat or question tools.
- Never run two MQTT clients with client ID pi-lab-01.

From the Pi deployment directory, the existing commands are:
- Tests: `.venv/bin/python -m unittest -v test_client test_firmware`
- Bounded client: `.venv/bin/python -u client.py --duration 600`
- Continuous client: `.venv/bin/python -u client.py` (Ctrl+C stops).

Logs go to stderr; no persistent application log exists by default. A user may
run through `tee` locally, but check for a running instance first. Current logs
show connection, subscription, telemetry PUBACK and final command status/ID.
Intermediate downloading status goes to MQTT; detailed per-block local progress
logging has not been implemented. Do not dump command payloads or signed URLs.

## Cloud interface to preserve

- AWS account: 844274146091; region: ap-southeast-2 (Sydney).
- Thing / client ID: pi-lab-01; policy: pi-lab-01-policy.
- Endpoint: an0l72m95csqi-ats.iot.ap-southeast-2.amazonaws.com:8883.
- MQTT 3.1.1 mTLS, active device certificate; clean session, QoS 1.
- Device may connect as pi-lab-01, publish telemetry/acks, subscribe/receive
  commands. Do not broaden it to let the device send its own cloud commands.

| Topic | Direction |
| --- | --- |
| lab/pi-lab-01/telemetry | Pi -> cloud |
| lab/pi-lab-01/commands | cloud -> Pi |
| lab/pi-lab-01/acks | Pi -> cloud |

Supported commands:
- `get_status`: command_id + command; returns Pi status and latest firmware stage.
- `stage_firmware`: exactly command_id, command, version, url; reports downloading
  then verified/error. Verified always means STAGED, not installed; flashed=false.
- A new attempt needs a new command ID. Exact duplicates replay cached results
  within this process; changed fields under the same ID return command_id_reused.
- Retain OFF; retained deliveries rejected. No shadow, IoT Jobs or portal yet.

Downloader pins local approval metadata, exact regional HTTPS S3 host/path and
signed versionId, checks response version, size and SHA-256, then atomically
replaces one fixed staging file. No redirects/proxy or remote-controlled output
path. Signed URL is transient, not persisted in approval or result/cache.
Changing the approved firmware requires an operator metadata update today.

Known lab limitations (not new defects): synchronous application download pauses
telemetry/command processing while SDK networking continues; queue 32 and replay
cache 128 are bounded. Socket timeout 5s, transfer elapsed check 30s (not a strict
DNS bound). MQTT publish timeout can exit the process. No durable attempt ledger,
offline status delivery, automatic restart, firmware signature verification,
anti-rollback or power-loss durability guarantee. SHA-256 is not authenticity.
Do not use a cached verified response as proof that a future flash operation ran.

## Exact approved artifact

- Board/MCU: NUCLEO-G0B1RE / STM32G0B1RETx (from CubeMX project).
- Release: 0.1.0, Debug; raw .bin.
- Flash base: 0x08000000; project flash size 512 KiB, RAM 144 KiB.
- Size: 19212 bytes.
- SHA-256: `e2c6d4bcf172cc803fa497a60bd0b625f5448a33433ab2d00bacec5fff6cc521`
- Initial stack pointer: 0x20024000; reset vector: 0x08004935.
- Bucket: iot-lab-firmware-844274146091 (private, ACLs disabled, Block Public
  Access enabled, versioning enabled, SSE-S3).
- Object: firmware/stm32g0b1/0.1.0/firmware.bin.
- S3 binary version: `vtuJfI9yuJtFx6EqWSkSpULFENCCgo54` (case-sensitive).
- Uploaded manifest is unsigned and does not contain the subsequently obtained
  S3 version ID; local release.json does. Do not silently overwrite the release.

Read the actual board identity and recheck image bytes before any future erase.
The staging file can be replaced by a subsequent staging operation: serialize
download/flash ownership and bind the bytes being programmed to verified
metadata. No concurrency lock or flasher interface exists yet.

## Hardware planning: unresolved, not wiring instructions

Existing application uses USART2 PA2/PA3 at 115200 8N1 and PWM TIM15_CH1 PB14.
**Application UART configuration does not establish ROM bootloader support.**
USART1 PA9/PA10 ROM support is document-verified. Current boot option registers
were read over ST-LINK at 06:55 UTC on 2026-11-14 (see milestone below):
nBOOT_SEL=1 means physical BOOT0 is currently ignored. No option bytes changed.
No Pi-to-STM32 wiring, bootloader handshake or Pi UART configuration has been
verified in this work.

Hardware chat should research official ST documentation (AN2606 device-specific
bootloader table, AN3155 USART protocol, the applicable reference manual and
NUCLEO board schematic/manual). Record document revision, section/table and URL.
Verify Raspberry Pi 3 UART/GPIO details for installed Pi OS as well.

Before proposing a final pin-to-pin table, establish:
1. Physical board and revision; connector pins and solder bridge/ST-LINK VCP
   routing, including possible contention on PA2/PA3.
2. ROM-supported UART instance/pins and serial framing, boot-selection option
   bytes and BOOT0/NRST availability; inspect current settings read-only first.
3. Pi UART device mapping, PL011/mini-UART/Bluetooth allocation, serial console
   conflicts and required permissions. Do not disable Wi-Fi/SSH.
4. 3.3 V logic, common ground, independent board power/back-power risks, safe
   reset drive (including open-drain considerations) and safe default boot state.
5. Compatible programmer/library on Pi aarch64, supported device ID, erase/write/
   readback commands, timeouts, error handling and recovery after interruption.

Proposed staged milestones (require user agreement, not already authorized):
1. Documentation-backed wiring/boot plan and read-only inspection.
2. Confirm wiring physically; enter ROM bootloader and read ID/version only.
3. Implement standalone Pi flashing module/CLI with explicit write confirmation,
   revalidation of exact bytes/target, exclusive access and clear progress/errors.
4. User-approved local erase/program/readback verification, restore normal boot
   and verify the application actually runs. ROM programming is not rollback.
5. Agree an explicit cloud install operation, idempotency and recovery contract;
   integrate with existing staging only after local flashing is demonstrated.
   Do not make stage_firmware automatically flash or conflate verified with
   programmed/booted/healthy.

## Toolchain and recovery constraints

- Build output: workspace build/Debug/G0B1_PWM_Practice.elf.
- Existing build task succeeded on 2026-11-14 (ninja no work to do).
- CMake integration API was unavailable that time; fallback existing build task
  worked. Prefer appropriate IDE build tools when available.
- `/usr/bin/arm-none-eabi-objcopy -O binary` generated the upload binary.
- Prior repo notes (2026-10-21): xPack OpenOCD at `/home/wh/tools/openocd` worked
  over ST-LINK/SWD with this board. Recheck physical connection before using.
  Laptop STM32CubeProgrammer 2.23/CubeIDE bundled programmer failed due to Ubuntu
  20.04 glibc/libstdc++ requirements. Do not assume it works on laptop or Pi;
  CPU architecture and dependencies must be checked separately.
- Preserve unrelated untracked `.venv/` and `.venv-1/` in STM32 workspace.
- Current 32 GB Pi card is temporarily repurposed. User requires eventual
  restoration of its original image AFTER saving lab work. Verified backup:
  `/media/wh/Elements SE/RaspberryPi-Backups/original-32GB-2026-11-14/original-card.img`
  Size 31914983424; SHA-256
  `cc670e2b02824a2e59d45fe7984e414924d89d78f8c5835f4641504782bf973e`.
  Restore notes/checksum alongside image. Do not overwrite this backup. Device
  names used during backup are historical, never assume current /dev/sdX.

## Secret handling

Certificate/key files already exist under `/home/pi/aws-iot/certs`; never display
private key contents or copy them into handovers, commits, or logs. Do not put
permanent AWS credentials on the Pi for this workflow. Presigned URLs grant
temporary object access and must not appear here.
The user's scratch notes can contain signed URLs: do not read/dump them wholesale
or attach them to a new chat. Prior GUI logs exposed Wi-Fi credential material;
never reproduce it. Enter secrets directly in trusted local UI/terminals.

## Cross-chat working agreement

1. User opens the hardware chat in the same workspace and attaches this file.
2. Hardware chat reads current source and this record, then proposes a plan.
3. Only ONE chat owns edits/deployment at a time. Current planned ownership:
   hardware chat after user starts it; cloud chat paused for hardware work.
   This is a human convention, not a software lock or automatic synchronization.
4. After each milestone update this document's current state and append a log:
   UTC date/time, chat/workstream, files changed (absolute laptop/Pi paths),
   hardware/config changes, exact non-secret test results, pending work, and
   process status. Record verified wiring/option bytes, not guesses.
5. Before returning to cloud chat, finish that entry and stop simultaneous edits.
   Ask cloud chat to reread this document and inspect changes, not rely on memory.
6. Keep credentials out. A separate chat is not a reporting subagent; files and
   explicit handovers carry state, not automatic conversation merging.

## Append-only milestone log

### 2026-11-14 — Cloud baseline / handover
- Created this handover and STM32-CHAT-STARTER.md; no hardware/runtime edits.
- Firmware delivery and independent Pi checksum verified as recorded above.
- Last client run exited cleanly at 05:15:43 UTC; no client at 05:19:19 check.
- Next: hardware planning only; ROM UART/pins/boot configuration still unverified.

### 2026-11-14 05:36:58 UTC — Hardware documentation review only
- Read local official manuals under `/home/wh/Downloads/resumes/prep_material/`:
   - `en.DM00371828.pdf`: RM0444 Rev 6, December 2024.
   - `um2324-stm32-nucleo64-boards-mb1360-stmicroelectronics.pdf`: UM2324 Rev 5,
      July 2024; explicitly covers NUCLEO-G0B1RE / MB1360.
- RM0444 section 2.5.2, page 69 confirms an ST factory-programmed system-memory
   bootloader for flash reprogramming. USART and I2C apply to all covered devices;
   STM32G0B1 also supports SPI, USB DFU and FDCAN bootloader interfaces.
   A custom STM32 bootloader is not required for the proposed Pi USART route.
- RM0444 section 2.5, Table 8, pages 68–69: pin-selected system-memory boot
   requires BOOT_LOCK=0, nBOOT_SEL=0, nBOOT1=1 and BOOT0 high. These are documented
   conditions, NOT a readout of this board or permission to write option bytes.
   BOOT0 sampling also depends on NRST configuration. Debugger access can force
   pin-selected BOOT0 low until the next power-on; account for this in planning.
- UM2324 section 8, Table 13, page 36 identifies PA14 as BOOT0/SWCLK. Do not
   attach a competing Pi output without resolving ST-LINK/SWD routing first.
- UM2324 section 6.9, Table 8 documents PA2/UART2_TX via SB16 and PA3/UART2_RX
   via SB18 connected to ST-LINK VCP by default; its external ST morpho use lists
   the bridges OFF. This establishes board routing, NOT ROM UART pin support.
- UM2324 section 6.8, page 24 documents active-low reset at CN6 pin 3 and CN7
   pin 14. Physical board revision, routing and electrical drive remain to verify.
- Official document URLs (local copies were used; online PDF retrieval failed):
   - https://www.st.com/resource/en/reference_manual/rm0444-stm32g0x1-advanced-armbased-32bit-mcus-stmicroelectronics.pdf
   - https://www.st.com/resource/en/user_manual/um2324-stm32-nucleo64-boards-mb1360-stmicroelectronics.pdf
- Changed only `/home/wh/STM32Projects/G0B1_PWM_Practice/docs/OTA-HANDOVER.md`.
   No deployment, wiring, GPIO, option-byte or Pi configuration changes.
- Checks: read-only local PDF extraction; no hardware tests, board readout or
   Pi process check. No MQTT client started. Failed PDF-download command finished.
- Pending: AN2606 device-specific UART pins/activation details, AN3155 protocol,
   physical board/revision and schematic verification, read-only option-byte and
   Pi UART checks. No final wiring table or flash operation authorized.

### 2026-11-14 06:08:08 UTC — Official schematic located / CN4 isolation identified
- ST's NUCLEO-G0B1RE CAD resources list MB1360-G0B1RE-C02 Board Schematic,
   version 1.0, dated 29 October 2020:
   https://www.st.com/resource/en/schematic_pack/mb1360-g0b1re-c02_schematic.pdf
   The listing and exact URL were verified online. PDF content retrieval failed;
   no schematic-sheet/component-level trace is claimed from that PDF yet.
- Local manuals now reside in `/home/wh/Downloads/resumes/prep_material/stm32/`
   (the parent-directory paths in the preceding historical entry have moved).
- UM2324 Rev 5 sections 6.4.3–6.4.4, pages 15–17, Figures 7–8 explicitly
   document the two CN4 jumpers: installed for onboard ST-LINK debugging;
   removed to disconnect onboard target SWD when debugging an external target.
   Together with Table 13 (PA14/SWCLK/BOOT0, PA13/SWDIO), this identifies CN4
   as the documented removable SWD connection, rather than modifying reserved
   SWD solder bridges. Exact CN4 contact orientation still needs diagram/board
   verification before a pin-specific instruction.
- CN4 isolation does not mean complete electrical separation: target power,
   reset and VCP UART paths are separate. Section 6.4.4 specifically says SB19
   must be OFF when CN11 pin 5 (NRST) is used for an external application.
   SB16/SB18 remain the separate PA2/PA3 VCP paths. Do not assume removing CN4
   disconnects reset or UART, or removes the debugger boot-selection latch.
- Proposed mode separation remains planning only: CN4 fitted with Pi BOOT0
   drive isolated for debugging; CN4 removed for planned Pi ROM updates, with
   reset/UART safety and power-cycle requirements separately resolved first.
   CN2 USB power does not require those two SWD jumpers to remain fitted.
- Changed only `/home/wh/STM32Projects/G0B1_PWM_Practice/docs/OTA-HANDOVER.md`.
   No hardware changes, flashing, deployment, tests on the board or Pi, or
   MQTT client starts. Pi process status not rechecked.
- Pending: physical board revision/photo, actual schematic inspection, AN2606
   UART pin table, option-byte readout and complete safe wiring plan.

### 2026-11-14 06:47:29 UTC — AN2606 USART1 route confirmed
- Downloaded and read AN2606 Rev 61 (January 2024, 436 pages). Official ST
   endpoint failed; a mirrored ST document was used, not claimed as latest:
   https://kolegite.com/EE_library/application_notes/MCU/STM32/STM32%20microcontroller%20system%20memory%20boot%20mode.pdf
   Local file: `/home/wh/Downloads/resumes/prep_material/stm32/an2606-stm32-microcontroller-system-memory-boot-mode-stmicroelectronics.pdf`.
   Size: 6516495 bytes; SHA-256:
   `9249cd380844d758e5826ff6ce488aad22ed128eaeccbbf34d6657d058ab2ab2`.
- Chapter 44, section 44.1, Table 95, page 220 explicitly confirms:
   - USART1: PA9 TX, PA10 RX.
   - USART2: PA2 TX, PA3 RX.
   - USART3: PC10 TX, PC11 RX.
   USART framing is 8 data bits, even parity, one stop bit (8E1), unlike the
   current application's 8N1. Figure 55, page 223 selects USART on receipt of
   0x7F. This is documentation evidence, not an observed handshake.
- Preferred planned Pi route: USART1 PA9/PA10, leaving SB16/SB18 and ST-LINK
   VCP untouched. The now-available local C02 schematic
   `/home/wh/Downloads/resumes/prep_material/stm32/mb1360-g0b1re-c02_schematic.pdf`
   was text-inspected for these nets: PA9 reaches CN5 pin 1 (D8); PA10 reaches
   CN9 pin 3 (D2). Pi TX would connect to PA10; Pi RX to PA9. No Pi-side pin
   mapping/configuration or physical connection is verified yet.
- Section 44.1 references Table 2 Pattern 11 for boot entry. Pin-selected
   system-memory boot requires BOOT0 high, nBoot1=1, BOOT_LOCK=0 and
   nBOOT0_SEL=0 (AN2606 spelling; RM0444 calls the selector nBOOT_SEL).
   Actual option bytes remain unread. Do not erase flash to exploit empty check.
- Table 96, page 224 lists bootloader V9.2 with no known limitations in this
   revision. Actual silicon/ROM version has not been read.
- Earlier user photo `/home/wh/Downloads/resumes/prep_material/stm32/nucleo_gpb1re.jpg`
   showed MB1360C marking and both CN4 caps fitted; full C02 assembly revision
   and electrical continuity are not verified by that photo.
- ROM also uses other peripheral pins: Table 95, page 222 lists PB14 as SPI2
   MISO and notes MISO is driven high after SPI initialization. PB14 is the
   application's PWM pin; account for any attached load before entering ROM mode.
- Changed this handover only in the workspace; the PDF download above was the
   only added reference artifact. No code/deployment, wiring, solder bridge,
   GPIO, option-byte or Pi serial changes; no board tests or MQTT client starts.
   Pi process status not rechecked.
- Next: resolve safe BOOT0/NRST control and read-only option-byte inspection,
   check Pi UART mapping and AN3155 host protocol, then agree a read-ID-only
   handshake. UART routing documentation is resolved; hardware operation is not
   yet tested. No erase/program operation authorized.

### 2026-11-14 06:52:25 UTC — Boot/reset inspection; board read blocked
- User authorized boot/reset planning and reading current boot option bytes,
   with wiring unchanged. No reset, halt, programming or option-byte write was
   performed. `lsusb` showed no ST-LINK/0483 device; debugger process search
   found none. Actual board register reads are blocked until CN2 is connected
   to the laptop with a data cable and CN4 remains fitted.
- Visually inspected local MB1360-G0B1RE-C02 schematic sheets 3 and 7:
   - PA14/BOOT0 connects to the target SWCLK path via CN4; connector sheet 5
      text identifies PA14 at CN7 pin 7.
   - U7 PF2/NRST connects to reset button B2 (pulls to ground) and C28 100 nF
      to ground. Do not confuse R29 (user-button PC13 pull-up) with a reset or
      BOOT0 pull-up.
   - U2 PB0/T_NRST connects via SB19 to target NRST, independently of CN4.
      R17 22 ohms is on the CN11 external reset branch, not a general isolation
      resistor between U2 and target NRST. Schematic alone does not establish
      the ST-LINK firmware's GPIO output mode.
- Verified CMSIS register offsets/fields and RM0444 reset modes for later reads:
   DBG_IDCODE at 0x40015800; FLASH_OPTR at 0x40022020; FLASH_SECR at 0x40022080.
   OPTR: RDP bits 7:0, nBOOT_SEL bit 24, nBOOT1 bit 25, nBOOT0 bit 26,
   NRST_MODE bits 28:27. SECR: BOOT_LOCK bit 16.
   NRST_MODE 01=input reset, 11=bidirectional reset, 10=GPIO, 00=reserved.
- OpenOCD target config has an examine-end hook that writes DBGMCU registers.
   For the planned register-only inspection override that hook with an empty
   event before init, disable unused servers, avoid reset/halt/program commands
   and do not use the flash task. Debug access itself can still affect the
   documented BOOT0 debug latch; allow for a later agreed power cycle.
- Proposed initial test approach: manual BOOT0 selection with CN4 isolated and
   existing B2 reset button, before adding automated Pi reset drive. Not yet a
   wiring instruction: option bytes and power-cycle procedure must be resolved.
   Later Pi reset should use a low-or-release driver (e.g. suitable open-drain
   stage), with ST-LINK reset sharing verified or isolated first. No component
   values/Pi GPIO choices approved yet.
- Changed only this handover; temporary schematic raster images were generated
   under /tmp for inspection. No gateway deployment or Pi access. No MQTT client
   started; Pi process status not checked.
- Next action required from user: connect laptop to CN2 with both CN4 jumpers
   still fitted; leave Pi wiring disconnected/unchanged and do not launch a
   debugger. Then perform the authorized non-resetting register reads.

### 2026-11-14 06:55:28 UTC — Current boot option registers read successfully
- User connected CN2. USB reports 0483:374b ST-LINK/V2.1; no competing debugger
   process found. OpenOCD STLINK V2J37M27, target voltage about 3.21 V,
   Cortex-M0+ detected. DBG_IDCODE=0x10016467, consistent with this G0B/G0C device.
- Register read results:
   - FLASH_OPTR (0x40022020) = 0xFFFFFEAA.
   - FLASH_SECR (0x40022080) = 0x00000000.
   - RDP=0xAA (level 0); nBOOT_SEL=1; nBOOT1=1; nBOOT0=1;
      NRST_MODE=3 (bidirectional reset); BOOT_LOCK=0.
- Conclusion: current configuration selects main flash through option bits;
   physical BOOT0 is ignored. Removing CN4 and driving BOOT0 high alone will
   NOT select ROM with these settings. Reset pin is already enabled and boot
   lock is not set. This is not a complete audit of all flash protection areas.
- For the proposed physical-BOOT0 route, the required selector change is
   nBOOT_SEL: 1 -> 0, preserving all unrelated bits. This is a proposed later
   option-byte write, NOT authorized/performed. Establish a stable low default
   on BOOT0 and an agreed isolation/power/reset procedure before enabling it;
   then high at reset selects ROM, low selects main flash under the verified
   nBOOT1=1 and BOOT_LOCK=0 settings. No NRST_MODE change is needed.
- Read method: xPack OpenOCD, board/st_nucleo_g0.cfg, unused servers disabled,
   reset_config none, examine-end event overridden with an empty body before
   init to suppress stock DBGMCU writes. No reset/halt/flash/program/option-byte
   write commands. Initial mdw output omitted values; a second connection using
   read_memory and explicit formatted echo produced the results above.
   Both commands exited 0; no debugger process remained at the final check.
   SWD access itself can affect the documented BOOT0 debug latch; a later
   agreed power cycle is still part of the planned transition to ROM testing.
- No wiring/GPIO/configuration/deployment changes. Changed only this handover.
   No Pi access or MQTT client starts; Pi process status not checked.
- Next: agree manual BOOT0/B2 test wiring and safe default boot state, then
   separately approve the nBOOT_SEL change and its option reload/reset.
   Keep CN4 fitted and Pi control wiring unchanged until the plan is agreed.

### 2026-11-14 09:58:22 UTC — Pi UART/GPIO read-only inspection
- SSH alias pi with strict host-key checking succeeded. Model reported Raspberry
   Pi 3 Model B Rev 1.2, aarch64, Debian 13 Trixie. User pi belongs to dialout
   and gpio; gpiochip devices are root:gpio 0660 and GPIO inspection succeeded
   without sudo. No UART tty device exists yet, so future UART permissions
   still need checking after configuration.
- Live DT serial0 -> /soc/serial@7e215040 (mini-UART), status disabled;
   serial1 -> /soc/serial@7e201000 (PL011), status okay. No /dev/serial*,
   /dev/ttyAMA* or /dev/ttyS* nodes, nor matching /sys/class/tty entries.
   Bluetooth hci0 is under 3f201000.serial in sysfs, confirming kernel-managed
   Bluetooth owns PL011. bluetooth.service active; no hciuart unit installed.
- Stored /boot/firmware/cmdline.txt has console=serial0,115200 and console=tty1;
   live command line expands serial0 to ttyS0. No active serial-getty instance;
   template disabled. The stored UART console must be removed before using the
   header for the STM32, even though its UART is currently disabled.
- /boot/firmware/config.txt active-directive inspection found no enable_uart,
   Bluetooth UART overlay, core_freq, force_turbo, gpio= or include directive.
   Section-filtered overlays for other Pi models are not active on this Pi 3.
- pinctrl get 0-27: header GPIOs currently inputs; GPIO14 reads low, GPIO15
   high, GPIO17 low. gpioinfo shows no consumers on lines 0–27. gpiochip0 is
   pinctrl-bcm2835, 54 lines; libgpiod 2.2.1 and pinctrl installed. Kernel
   consumers on internal Bluetooth/Wi-Fi/SD pins must not be disturbed.
   No consumer is not proof of no physical wiring, and observed low is not a
   guaranteed startup/default-low circuit. GPIO17 (physical pin 11) is a proposed
   BOOT0 candidate pending physical wiring/default-state agreement.
- Official Raspberry Pi documentation, Configure UARTs / UART types, confirms
   mini-UART has no parity bit. STM32 ROM requires 8E1; enable_uart=1 alone
   with the present mapping is insufficient. Use PL011 for this GPIO UART route.
   UART TX=BCM14/physical pin 8; RX=BCM15/physical pin 10.
   https://www.raspberrypi.com/documentation/computers/configuration.html#configuring-uarts
- Installed /boot/firmware/overlays/README confirms disable-bt restores
   UART0/ttyAMA0 to GPIO14/15; miniuart-bt does the same while moving Bluetooth
   to the lower-bandwidth mini-UART (krnbt parameter supported).
- Proposed simplest configuration, NOT applied/authorized: back up boot files,
   add dtoverlay=disable-bt and enable_uart=1 in applicable scope, remove only
   console=serial0,115200 from the single-line cmdline (retain console=tty1 and
   all unrelated parameters), then approved reboot. This disables Bluetooth,
   not Wi-Fi. No blind hciuart service command: this system uses kernel Bluetooth.
   If Bluetooth must stay available, instead plan miniuart-bt with documented
   fixed core clock and reduced Bluetooth bandwidth. Ask user which tradeoff
   is acceptable before changes. Reboot briefly interrupts SSH; preserve network
   configuration and verify SSH return, PL011 device/aliases, pinmux, console
   absence and device permissions afterward. Do not connect STM32 yet.
- No configuration changes, pin direction/value changes, UART opens, package
   installs, reboots, STM32 accesses or firmware writes. No gateway files edited
   or deployed. Process-name check returned zero Python client.py processes;
   none started. Only workspace docs/OTA-HANDOVER.md and repo memory updated.
- Next: user decision on Bluetooth/PL011 assignment and explicit approval of
   serial boot changes/reboot, then continue wiring/default-low planning and
   the previously ordered read-ID-only steps. STM32 nBOOT_SEL still unchanged.

### 2026-11-14 10:12:52 UTC — approved PL011 setup and reboot verified
- User explicitly approved disabling Bluetooth, routing PL011 to GPIO14/15,
   enabling UART, removing the serial console and rebooting, preserving Wi-Fi.
- Backups on Pi: /boot/firmware/uart-backup-20261114T101156Z/ contains original
   config.txt and cmdline.txt. SHA-256 matched originals before edits:
   config.txt = 90a446821551a9df109ec1a77a79f6518fe974517d982d371c5e9efd6bbc98c3;
   cmdline.txt = 4890d1dc492199bc7c90c1dfa582acf89c19cc397b8d525f9636e76d8f9526ca.
- Added a comment, dtoverlay=disable-bt and enable_uart=1 after existing [all]
   in /boot/firmware/config.txt. Removed only leading console=serial0,115200
   from /boot/firmware/cmdline.txt; retained console=tty1, root, filesystem,
   repair, rootwait and regulatory-domain settings. Diff reviewed before reboot.
   Original cmdline had no trailing newline; preserved (wc -l reports 0 but
   the file still contains one valid nonempty logical line).
- Reboot issued after sync; SSH disconnection expected. Boot ID changed from
   820447c4-bef9-43b0-974b-e2dd24defeb7 to
   7727fc8d-7aea-42ac-96c2-ef6877acb4a4. SSH reconnected successfully;
   nmcli reports wlan0 wifi connected. No network configuration edited.
- /dev/serial0 -> ttyAMA0. /dev/ttyAMA0 root:dialout mode 0660; pi has
   read/write access. DT serial0 -> /soc/serial@7e201000, status okay.
   pinctrl: GPIO14 ALT0 TXD0 high; GPIO15 ALT0 RXD0 high. GPIO17 remains input
   low. /dev/ttyS0 also exists but is NOT the header UART to use.
- Live command line has only console=tty1; /proc/consoles lists tty1 only.
   No loaded Bluetooth/hciuart/serial-getty units; /sys/class/bluetooth absent.
   Onboard Bluetooth disabled by overlay; no separate hciuart service needed.
   Unprivileged fuser showed no UART users (recheck ownership before future use).
- No UART opened or bytes transmitted; 8E1 baud/framing not yet configured by
   a programmer. This verifies OS routing/availability, not STM32 communication.
   No GPIO17/BOOT0 drive, STM32 access, option writes or flash operations.
   Gateway process-name count remains zero; cloud code/staged image untouched.
- Rollback if later authorized: restore both boot files from the backup above,
   then reboot. Do not restore automatically or overwrite intervening changes.
- Next: finalize safe wiring and BOOT0 default-low bias, then prepare bounded
   read-ID helper with manual B2 reset. nBOOT_SEL still needs separate approval.
- Requested PL011 PDF download was paused by user after official asset-host
   DNS resolution failed; no PDF downloaded or page citations established.

### 2026-11-14 — Nucleo reconnection and boot-option recheck
- User confirmed both CN4 caps fitted, no Pi wires or BOOT0 jumper, and no
   external PB14 load. ST-LINK detected; no active debug-tool process found.
- Repeated register-only OpenOCD inspection with examine-end hook disabled;
   no reset/halt/erase/program commands. Target voltage 3.210874 V.
   DBG_IDCODE=0x10016467, FLASH_OPTR=0xFFFFFEAA, FLASH_SECR=0 unchanged.
   nBOOT_SEL remains 1; physical BOOT0 ignored. OpenOCD exited successfully.
- User has no resistors and agreed to supervised manual BOOT0 selection plus
   B2 reset instead of Pi GPIO17 control. No wiring/option changes performed.
   Physical BOOT0 enable and option reload/reset still require separate approval.
   Read-ID helper and exact manual-jumper/power procedure remain to be prepared.

### 2026-11-14 10:39:06 UTC — nBOOT_SEL-only change and flash preservation verified
- User explicitly approved nBOOT_SEL 1 -> 0 plus option reload/reset. Confirmed
   CN4 fitted, no Pi wires/BOOT0 jumper and no external PB14 load before work.
- Installed OpenOCD help verified stm32l4x option_write bank offset value mask
   and option_load bank. Upstream driver source confirmed masked read/modify/write.
   Identity and starting OPTR/SECR checked immediately before programming.
- Briefly halted and dumped full 512 KiB at 0x08000000 before modification;
   backup: build/boot-option-backup/flash-before.bin. CPU resumed after backup.
- Issued exactly one option write: stm32l4x option_write 0 0x20 0x00000000
   0x01000000. Immediate register read still showed 0xFFFFFEAA, so a defensive
   pre-reload check stopped that session. No repeated option write was issued.
   RM0444 option-loading description reviewed, then a separate session issued
   stm32l4x option_load 0 (approved reset). Do not treat pre-reload register
   readback alone as evidence of programming failure or blindly repeat writes.
- Fresh connection after reload: OPTR=0xFEFFFEAA, SECR=0. Only OPTR bit24 changed;
   nBOOT_SEL=0, nBOOT0=1, nBOOT1=1, NRST_MODE=3, RDP=AA, BOOT_LOCK=0.
- Dumped build/boot-option-backup/flash-after.bin; cmp passed against before.
   Both full-flash SHA-256 values:
   e46721beb7d9465aeb237c76f6dfa1bcfd584c4986578183bc7c66f582c9a5a5.
   No application erase/program commands issued. Option programming is distinct
   from application flash programming. Final verification resumed CPU/exited 0.
- CN4/BOOT0 wiring unchanged. SWD access still means a full target power cycle
   is needed before pin-selected ROM test; B2 alone is insufficient to clear the
   debugger BOOT0 latch. Do not reconnect SWD after that power cycle.
- No ROM handshake, UART data or observable application health check yet.
   Next prepare read-ID helper and exact manual-jumper/power procedure before
   powering down, removing CN4 and wiring. Do not leave BOOT0 undefined at boot.

### 2026-11-14 19:37:32 UTC (Nov 15 local) — standalone read-ID helper ready
- User requested preparing/testing the helper only, no further photo review.
   Reports ground correction done. Pi previously shut down on request; user
   reports both boards unpowered, common ground, CN4 removed, BOOT0-to-target-3V3,
   TX/RX disconnected and GPIO17 disconnected. Separate supplies: laptop USB
   for Nucleo and adapter for Pi. This is reported, not independently verified.
- Added files to canonical laptop gateway /home/wh/Downloads/resumes/plasmaleap/aws/pi-client:
   rom_read_id.py, test_rom_read_id.py, requirements-hardware.txt, ROM-READ-ID.md.
   Existing client.py/firmware.py/cloud requirements and stage_firmware unchanged.
- Exact transmit sequence: 7F, 00 FF, 01 FE, 02 FD. Only 00/01/02 command
   allowlist; no programming/erase/unprotect/GO or memory access. Verify ACKs,
   length framing, supported commands, matching version replies, PID=0x0467.
   Output status=rom_identified, flashed=false, application_health_verified=false.
   PID is a family identifier, not unique board identity. Version not hardcoded.
- Linux/Pi-specific CLI: /dev/serial0 must resolve to /dev/ttyAMA0; default
   115200 8E1, no flow control, false DTR/RTS, pySerial exclusive=True (advisory,
   not protection against noncooperating openers). READY confirmation before
   opening; stale-input clear then manual B2/RESET prompt before any bytes.
   Ctrl+C/EOF cancels; error closes port; no retries or blocking drain.
   Per-write timeout and shared per-command response deadline (2 s default,
   0.1–10 s allowed). Human prompts intentionally await operator confirmation.
- pyserial==3.5 is a separate hardware dependency; not installed on Pi yet.
   Initial local venv setup lacked ensurepip; package tool reported success but
   imports still failed. Actual test dependencies installed via pip --target into
   workspace build/read-id-test-deps; use PYTHONPATH pointing there with
   /usr/bin/python3 (3.8.10) from the laptop gateway folder for tests.
- Final command: PYTHONPATH=/home/wh/STM32Projects/G0B1_PWM_Practice/build/read-id-test-deps
   /usr/bin/python3 -m unittest -v test_rom_read_id test_client test_firmware.
   Result: 55 passed (26 helper, 11 client, 18 firmware). CLI --help passed.
   Synthetic V9.2 transcript, truncation at every position, ACK/NACK/noise,
   missing commands, wrong ID/version, bounds, cancellation, I/O failures,
   allowlist, real pySerial PTY transport/timeout/advisory lock tested.
   Linux PTYs cannot validate physical parity (8N1 simulation only); separate
   configuration test asserts 8E1. Runtime termios errors handled cleanly.
- ST AN3155 PDF current/legacy URLs failed (HTTP/timeout); alternate attempts
   also failed. No valid AN3155 PDF or verified page citations obtained.
   Protocol cross-check: https://github.com/stm32duino/stm32flash/blob/main/stm32.c
   (command definitions, stm32_init and Get ID parsing). AN2606 pins/8E1 evidence
   remains as previously recorded. Tests do not replace a real ROM handshake.
- No Pi SSH, hardware UART, GPIO, SWD, MQTT, deployment or flashing performed
   in this preparation. Next: power Pi with UART disconnected, selectively deploy
   helper + hardware dependency into existing /home/pi/aws-iot/.venv, then agree
   exact supervised power/UART/B2 test sequence. No automatic test on deployment.

### Hardware chat next entry (fill after work; do not mark planned tests passed)
- UTC timestamp / workstream:
- Decisions and supporting documentation:
- Files changed / deployed:
- Physical wiring and option-byte/config changes (before -> after):
- Tests performed and observed results:
- Current running processes:
- Pending work / blockers / handback instructions: