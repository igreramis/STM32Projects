# Cloud-triggered ROM installation (manual BOOT0/B2)

## Implementation status

Implemented locally and now included in version control under
[gateway/pi-client](../gateway/pi-client/README.md). This snapshot was imported
from /home/wh/Downloads/resumes/plasmaleap/aws/pi-client; future tracked changes
belong in the repository copy. The two directories do not automatically sync.
**Deployed to Pi; service activation and live MQTT/hardware trial pending.**
See [deployment/trial log](CLOUD-INSTALL-TRIAL.md). The Pi suite now has 310 tests
after correcting a test-only serial-symlink assumption; all 310 pass.
309 software tests passed from the repository copy on host Python 3.8.10,
including simulated ROM,
heartbeat, journal/restart, and MQTT future/failure tests. Simulation does not
prove electrical timing or a real cloud-to-board installation.

The original `stage_firmware` operation remains download-only. Installation is
an explicitly enabled, separate `install_firmware` command. No GPIO, reset,
option-byte write, automatic boot, automatic reflash, or rollback is performed.

## Provisioning and enabling (not performed)

Gateway additions are ota_installer.py, ota_jobs.py, their tests, and
test_client_ota.py. client.py integrates the coordinator; rom_read_id.py exposes
its existing sync as an overridable method. Keep the existing firmware.py,
rom_program.py, rom_local.py and heartbeat_monitor.py dependencies together.

The gateway directory contains ota-config.example.json and
pi-iot-ota.service.example. These are templates, not active configuration.
Copy the JSON example to ota-config.json beside the deployed client and review:

| Configuration | Required local resource |
| --- | --- |
| `catalog` | Approved local-releases.json |
| `release_root` | All three approved local version directories |
| `baseline` | Original pinned full-flash local-baseline.bin |
| `run_root` | Private backup/readback and event directory |
| `state_root` | Persistent private deduplication/result directory |
| `port` | /dev/serial0 resolving to /dev/ttyAMA0 (PL011 only) |
| `download_releases` | Optional version-to-approved-S3-metadata path mapping |

Relative paths resolve against the configuration directory. Preserve the state
directory across restarts and deployments. Use the same state root for this
device; different roots defeat the process-level job lock. Protect configuration,
approval metadata, firmware, baseline and state against untrusted local writes.
The Pi user needs UART access (already a member of dialout in this setup).

Start the existing client with the additional argument `--ota-config` pointing
to that JSON file. Without it, install commands remain unsupported. The service
template supplies this argument, unbuffered journal output and the existing
gateway working directory. It must be reviewed/copied to a real systemd unit
before `journalctl` can show service logs. Ensure no other client uses
`pi-lab-01`; stop any Pi UART monitor or interactive programmer before starting.
Do not use the service template until the deployment/configuration is ready.

The service requests graceful cancellation and deliberately has no SIGKILL stop
deadline. Existing flash operations are bounded but cancellation is not checked
after every ROM transaction; stopping may take several minutes. Never kill,
reset or power off during programming. Restarting the service reports unfinished
jobs as interrupted; it does not resume them.

## Authorization and command contract

Use the existing MQTT command topic `lab/pi-lab-01/commands`, non-retained.
Only trusted AWS principals should have `iot:Publish` permission on this topic.
Gateway mTLS authenticates the gateway to AWS; it does not identify the original
publisher inside the received JSON. No JSON `authorized` field or hash alone is
an authorization credential. Local opt-in, publisher ACLs, exact image approval,
expiry and the known predecessor collectively gate destructive work.

Required fields:

| Field | Meaning |
| --- | --- |
| `command_id` | Unique 1–64 character ID; letters/digits/underscore/dot/colon/hyphen |
| `command` | `install_firmware` |
| `version` | Approved local `0.1.0`, `0.2.0` or `0.3.0` |
| `from_version` | Actual predecessor: one of those versions or `original` |
| `sha256` | Exact target binary SHA-256 from the local catalog |
| `expires_at` | Integer UTC Unix seconds: future, no more than 1800 seconds ahead |
| `url` | Optional signed URL matching separately provisioned S3 metadata |

Expiry is checked on acceptance, before UART access, and immediately before
erase. Use a fresh ID for each intentional attempt. An exact duplicate returns
the existing result without installing again, even after restart/expiry. Changing
any field under an old ID is rejected. Busy jobs are rejected, not queued.

Example envelope (placeholders deliberately prevent accidental submission):

```json
{
  "command_id": "trial-unique-id",
  "command": "install_firmware",
  "version": "0.3.0",
  "from_version": "0.2.0",
  "sha256": "f5f2f9c3c24f9f2f3e9931ad78446938454dd303bea4d866ae1b076dfb81ebbb",
  "expires_at": "REPLACE_WITH_FUTURE_INTEGER_UTC_EPOCH"
}
```

Do not infer the current predecessor from this example. The user has already
reported a successful local 0.3.0 flash; current application/wiring state needs
confirmation before a live trial.

Without `url`, installation uses the provisioned approved binary. With `url`,
the worker downloads to its private run directory and verifies exact S3
bucket/key/version ID, size and hash using the existing downloader. The original
staging slot is untouched. The approval catalog still requires all three local
images even when downloading. This is not arbitrary-release discovery: a new
release requires deliberate local approval and software/catalog changes.
The old cloud 0.1.0 binary is NOT the rebuilt versioned local 0.1.0 binary;
existing cloud release metadata must not be reused for it. No new S3 objects or
download approvals have been provisioned by this implementation.

## Operator workflow

Keep the existing safe wiring: both boards powered, common ground, crossed Pi
UART, CN4 caps removed, BOOT0 using only target-owned 3.3 V/GND. No Pi NRST wire.
Disconnect UART before powering either board off to prevent backpower. A separate
ST-LINK UART viewer may remain open receive-only; do not type into it during ROM
selection. Do not start a second program on the Pi UART.

1. An authorized install command is accepted and the image is validated.
2. Log: **Waiting for ROM mode: set BOOT0 HIGH and press/release B2.**
   Set BOOT0 to target 3.3 V, then press/release B2. No typed confirmation.
   The worker sends at most 60 sync probes over 120 seconds. Noise is bounded;
   NACK/ambiguous sync stops the job. After sync, protocol errors are not retried.
3. Identity/options/geometry and the full 512 KiB predecessor are checked. A
   private fsynced backup must match the explicitly selected known predecessor.
   The worker automatically authorizes erase only after both durable journals
   record intent. It erases pages 0–9, checks erased bytes, programs and verifies
   all flash, including the untouched tail. **Do not touch BOOT0/B2/power here.**
4. The ROM UART is closed, the UART is reopened at application 115200 8N1 and
   stale input is cleared before the next prompt (avoids losing fresh heartbeats).
   Log: **Flash verified. Set BOOT0 LOW and press/release B2.**
   Move BOOT0 to target GND, then press/release B2.
5. Two fresh, exact heartbeats with the expected version must arrive within
   120 seconds. Only then does the gateway report `application_verified`.
   This verifies version/main-loop/UART liveness, not complete PWM health.

No software can directly observe your jumper or button action in this wiring;
ROM replies and application heartbeats are the evidence. No power-cycle is
normally needed without intervening SWD; SWD debug use requires separate review
of the known BOOT0 latch behavior.

## Results, failure and recovery

Progress/final snapshots publish to `lab/pi-lab-01/acks` at QoS 1. They contain
`command_id`, `device_id`, `version`, `status`, `phase`, `event_seq`, `flashed`,
`flash_verified`, `flash_may_be_modified`, `application_health_verified`, and
`error` on failure. Progress may coalesce to the latest revision; it is not a
complete cloud event stream. The full backup/readback and hashes remain local
in the run directory. Signed URLs are not stored in the coordinator journal.

Delivery is marked only after PUBACK; an older PUBACK cannot acknowledge a newer
revision. Pending results survive reconnection/restart. PUBACK proves AWS broker
acceptance, not processing by a downstream cloud application. Use a fresh
`get_status` command ID to avoid the existing status replay cache.

| Evidence | Interpretation/action |
| --- | --- |
| `flashed=false`, no modification intent | Failure before destructive work; check fixed error/phase |
| `flashed=null`, `flash_may_be_modified=true` | Flash uncertain; keep BOOT0 HIGH, do not boot; manual recovery required |
| `flash_verified=true`, application timeout/error | Readback passed; application startup not confirmed; no automatic reflash |
| `application_verified`, health flag true | Two expected-version heartbeats received after flash verification |
| `interrupted` | Restart detected unfinished job; never automatically resumes |
| `storage_error` | Fail closed; fix/preserve journal before new jobs |

Erase intent is conservative: a crash just before the erase command can still
leave an uncertain result. Do not bypass predecessor checks to recover a partial
flash. No automatic rollback or restoration is implemented.

The journal retains up to 1024 command tombstones and rejects new IDs when full.
Do not delete records or change state roots to make a replayable command fresh.
There is no automatic pruning; capacity recovery requires an explicit offline
retention/replay-security procedure. Archive run backups separately without
discarding command tombstones. Preserve original baseline and all recovery data.