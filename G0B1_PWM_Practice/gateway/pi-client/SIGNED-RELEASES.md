# Signed releases and UART telemetry (gateway only)

Implements the gateway portion of [the admin contract](../../docs/ADMIN-MVP-CONTRACT.md).
The mandatory single-use-arm policy is now deployed; see
[current deployment evidence](../../docs/ADMIN-SINGLE-USE-ARM-DEPLOYMENT.md).
Both emergency master gates are enabled, but each installation requires a new
signed, unexpired, single-use authorization. The earlier supervised signed 0.4.0
installation and button checks are recorded in [the trial](../../docs/ADMIN-OTA-040-TRIAL.md).
Keep the existing `pi-iot-ota.service`, its identity/certificates, staging approval,
local release catalog/images, original baseline, run backups and OTA journal.
Do not start another client with the same MQTT client ID or another UART console.

## Exact proposed configuration

[ota-config.signed.example.json](ota-config.signed.example.json) is a separate,
deliberately non-runnable example until its signer ARN is provisioned. Do not
overwrite a live configuration with it. Merge the optional fields below into
the existing OTA configuration, preserving all existing paths and download approvals:

- `uart_monitor`: boolean `true` (absent defaults to `false`). Enables this
  process's receive-only 115200 8N1 PL011 monitor. `/dev/serial0` must resolve to
  `/dev/ttyAMA0`; mini-UART is refused. Stop any separate UART monitor first.
- `install_enabled`: boolean (absent defaults to `true` for existing installations).
  Set `false` during telemetry-only commissioning. New install requests are rejected
  with `deployment_disabled` before journal writes or worker creation. Existing
  duplicate job results remain available; this is not cancellation of active work.
- `require_deployment_arm`: strict boolean (absent defaults to `false` for legacy
  configurations). `true` requires `signed_releases` and a valid signed single-use
  deployment permit on every new install. A supplied permit is always checked,
  even when this flag is false; without a configured verifier it is rejected.
  The signed example enables this policy but keeps `install_enabled: false`.
  Both a valid arm and `install_enabled: true` are necessary for new work.
- `signed_releases`: object containing **exactly**:
  - `public_key`: local RSA-2048 public PEM path, proposed `release-signing-public.pem`.
    Provision through a trusted channel; no private signing key belongs on the Pi.
  - `key_id`: exact full KMS **key ARN**, not an alias. Replace
    `REPLACE_WITH_EXACT_KMS_KEY_ARN` with the approved signer ARN; it must match
    both the locally pinned PEM and every envelope. No network key discovery.
  - `bucket`: `iot-lab-firmware-844274146091`.
  - `region`: `ap-southeast-2` (also the KMS ARN region).
  - `seed_version`: **operator-confirmed, already full-readback-verified**
    installed catalog version (`0.1.0`, `0.2.0`, `0.3.0`) or `original`.
    The example's `0.3.0` is illustrative, **not a claim about current hardware**.
    Never seed an arbitrary dynamic version or infer the seed from telemetry.

Existing required keys remain `catalog`, `release_root`, `baseline`, `run_root`,
`state_root`, `port`; existing optional `download_releases` remains supported.
Relative paths resolve against the OTA configuration directory, not the working
directory. Keep the existing `state_root` to preserve job tombstones and pins.
Unknown fields, malformed signing configuration and non-boolean monitor flags
are rejected. Configuration construction does not open UART or connect to AWS.

In the separate gateway connection configuration, retain the existing endpoint,
client identity, certificates and `telemetry_interval_seconds: 10`. The cloud
stale thresholds are 35 seconds for both gateway and subdevice. No new MQTT
topics, device credentials, IAM permissions, service names or automatic boot
controls are introduced by this integration.

Install [requirements-signed.txt](requirements-signed.txt) in the Pi's existing
environment **in addition to** [requirements.txt](requirements.txt) and
[requirements-hardware.txt](requirements-hardware.txt). The signing requirement
is `cryptography>=43`; verify the chosen version on the deployment interpreter.
The existing [service example](pi-iot-ota.service.example) is unchanged: stdout
and stderr go to journald, and shutdown never SIGKILLs a flashing worker.
BOOT0/B2 prompts remain local stdout/journald only, never in MQTT job results or
telemetry. These docs authorize no physical operation.

## Signed command and verification

`install_firmware` requires `command_id`, `command`, `version`, `from_version`,
`sha256`, `expires_at`, plus `url` and `release` for dynamic signed installation.
`release` has exactly `key_id`, `payload`, `signature`. Both byte strings are
base64 encoded. The payload is canonical UTF-8 JSON with sorted keys and compact
separators, containing exactly:

`schema`, `target`, `version`, `size_bytes`, `sha256`, `bucket`, `region`, `key`, `version_id`.

- Schema is integer 1; target is `stm32g0b1`; numeric semver has three components,
  no leading zeroes, prerelease or build suffix (up to nine digits per component).
- Verify original payload bytes with RSA-2048 PKCS#1 v1.5 SHA-256 before JSON
  parsing; this matches KMS `RSASSA_PKCS1_V1_5_SHA_256`, `MessageType=RAW`.
  Reject duplicate JSON keys, noncanonical payloads and any unpinned ARN/key.
- Image is 8..20480 bytes; hash is lowercase SHA-256; key is exactly
  `firmware/stm32g0b1/<version>/firmware.bin`; a real S3 version ID is required
  (`null` is refused). The HTTPS URL must select that exact host/key/version.
  Redirects/proxies are disabled; response version, size and content hash must match.
- Revalidate vectors and embedded `firmware=<version>` tokens before UART opens.
  A signed release cannot silently replace an existing legacy catalog version.
- Command is bounded to 8192 UTF-8 bytes, URL to 3500 characters; expiry must be
  in the future and at most 30 minutes away. Expiry/cancellation are checked
  again before UART open and erase. Once erase starts, complete the bounded
  safety operation rather than treating expiry as a request to abandon writes.
- Old locally approved commands retain their existing version/approval policy.
  Arbitrary new numeric versions require signing; they need no catalog rewrite.

URLs and envelopes are transient worker inputs, not persisted command contents.
The journal retains a request fingerprint and fixed public status/error fields.
QoS redelivery returns the existing durable job; it never repeats an install.
Reusing an ID with changed fields conflicts. There is no automatic retry/rollback.

## Short-lived dashboard deployment arm

After one-time trusted provisioning, the dashboard calls
`POST /api/deployment-arms` and then `POST /api/deployments`, using the same
`{device_id, release_id, request_id}`. Arming returns metadata, not a download or
installation. The backend requires an existing arm for deployment and publishes
its signed envelope as `deployment_arm`. No per-update SSH/configuration toggle
is part of this flow. Physical BOOT0/B2 supervision remains unchanged.

The extra command field is allowed **only alongside `url` and `release`**. Its
envelope has exactly `key_id`, `payload`, `signature`, using the same locally
pinned RSA-2048 key/ARN and PKCS#1 v1.5 SHA-256 as releases. Original decoded
bytes are signature-verified before JSON parsing and must then equal canonical
JSON. The payload has exactly:

| Field | Required value |
| --- | --- |
| `schema` | integer `1`, not a boolean or float |
| `purpose` | `deploy_firmware` |
| `command_id` | canonical lowercase hyphenated UUID, equal to command ID |
| `gateway_id` | locally configured MQTT client ID (`pi-lab-01` here) |
| `device_id` | `stm32g0b1-01`, the gateway's supported subdevice |
| `version`, `from_version` | numeric semver, exactly matching command fields |
| `sha256` | lowercase SHA-256, exactly matching command field |
| `release_sha256` | lowercase SHA-256 of canonical JSON of the complete `release` envelope |
| `issued_at` | nonnegative integer epoch seconds |
| `arm_expires_at` | exactly `issued_at + 120` |
| `expires_at` | exactly `issued_at + 900`, equal to command expiry |

The coordinator verifies cryptography, exact types, identity and bindings before
any new journal consumption, worker start, monitor handoff, UART or download.
Admission uses a fresh clock sample **after verification**:
`issued_at <= now + 1` and `now < arm_expires_at`. Only future-issued timestamps
get a maximum one-second skew tolerance; expiry has none. No unsigned flag,
expiry, identity, or release approval substitutes for the signed permit.

Invalid/missing permits produce transient `invalid_deployment_arm` with no
worker or journal entry. Authentic, correctly bound expired permits instead get
a durable `rejected` tombstone with `deployment_arm_expired`, a version, false
verification flags and a positive `event_seq`, including arrival after command
expiry. The backend must accept this terminal error to clear its matching lease.
Existing storage/capacity/closed/emergency-master gates still fail closed.

The command UUID **is** the single-use arm ID. Existing non-evicted journal
records consume it atomically before thread start; no second ledger or reseeding
is needed. Identical ID/fingerprint redelivery returns its existing result even
after expiry, restart or master disable. Changed fields under an existing ID
conflict, and changing the ID cannot reuse the signed permission. A restart
marks interrupted work rather than executing it again. Protect and retain this
journal: deleting it destroys the single-use guarantee.

The 120-second deadline is **admission only**. After durable acceptance it is
not rechecked during backup/programming. The existing signed 900-second command
deadline is still checked at worker entry and before UART/erase; after erase,
bounded programming and full readback continue rather than abandoning flash.
`from_version` remains checked against the installer's persisted predecessor
and full target backup. No automatic boot, retry, rollback, or new GPIO behavior.
Only fingerprints/status are persisted, never permit payloads/signatures or URLs.

## Predecessor persistence and flash safety

The existing validated catalog and pinned **original 524288-byte baseline** remain
mandatory, including all three legacy images. Initial seed comes only from those
validated local inputs. A newly read target backup is never adopted as trust.

State is stored at `state_root/verified-flash/predecessor.json`, with a sibling
`state_root/.verified-flash-initialized` marker. Both are private local state.
The marker is fsynced before initialization; valid state from the earlier
implementation gains a marker without changing its predecessor pin.

Before erase, the complete target backup must match the persisted version's
full-flash hash, and `from_version` must equal the persisted version. Erase intent
is durably marked `pending` before the destructive command. Only the engine's
successful full readback followed by the expected full-flash hash check may
persist a new `verified` version/hash. Application heartbeat failure after that
does not undo the verified flash evidence.

Expected flash is signed image + FF padding to **20480 bytes** + the original
baseline tail. Erase stays **pages 0–9 only**, writes are 8-byte aligned, and all
512 KiB are read back and compared. Wrong predecessors, changed tails, failed
storage, pending/corrupt state, or a missing initialized directory fail closed.
No retries, rollback, mass erase, option writes, GO, GPIO or reset are added.

Never delete/recreate the journal, verified directory or initialization marker to
"fix" a failed install. Preserve them with run backups for operator investigation.
Deletion of **all** local state including the marker cannot be distinguished from
first provisioning; protecting/backing up that state remains an operator duty.
The schema is integrity-checked, not an authenticated defense against root-level
local file tampering. Do not disable signing/persistence after dynamic deployment
as a recovery workaround.

## UART telemetry and ownership

Each MQTT telemetry message retains the existing top-level fields and adds a
process-unique `boot_id` plus one `subdevices` entry with exactly:

`device_id`, `firmware`, `state`, `sequence`, `last_seen`, `age_seconds`, `updating`.

Absent UART evidence yields null observation fields. `last_seen` changes only
when a complete validated UART line arrives; cloud publishing never refreshes
it. `age_seconds` uses a monotonic clock so wall-clock corrections do not make
old observations fresh. The cloud must evaluate age as well as timestamp.

Accepted input is an exact legacy version heartbeat or a state JSON line with
exact fields `type`, `device_id`, `firmware`, `state`, `sequence`. Identity is
`stm32g0b1-01`, type is `state`, states are `idle`, `ready`, `running`, `paused`,
`fault`, sequence is uint32. Lines are bounded to 512 bytes; overflow discards
the whole line, not a valid-looking suffix. Counters may wrap/repeat across
reboot and therefore are **not** deduplication identifiers.

A same-firmware heartbeat retains a recently observed state, but cannot extend
its 35-second freshness. Expired state/sequence become null even if heartbeats
continue. Firmware changes and monitor handoff invalidate operating-state
freshness. MCU initial state, stable button handling and 10-second emissions are
firmware responsibilities outside this gateway-only work.

The monitor must synchronously stop, join and confirm UART close before the
installer executes. The installer owns ROM 8E1 and application verification 8N1
sequentially, then the monitor resumes after confirmed close. Close/open
uncertainty inhibits further opens. Shutdown joins the worker and closes the
monitor **before releasing the lifetime OTA lock**. `updating` also reflects an
accepted/active worker before it has acquired UART; it does not imply fresh data.
Read failure closes the monitor without an automatic reopen loop; observations
age out. Investigate and restart deliberately if monitoring has stopped.

## Offline verification and remaining gates

[run_tests.py](run_tests.py) discovers the full suite but excludes four existing
PTY transport cases, blocks real serial opens, PTY creation, socket connection,
DNS and MQTT connect/publish. It uses temporary synthetic baselines, ephemeral
RSA test keys, mocked HTTPS/MQTT and the independent in-memory ROM emulator.
Ordinary unittest discovery still includes all legacy PTY tests; none were removed.

Tested locally with `/usr/bin/python3` (Python 3.8.10), existing test dependencies
from `build/read-id-test-deps`, pySerial 3.5, awscrt 0.36.1 and system cryptography
39.0.2. That cryptography version exercises the required API/algorithms but is
**below the declared deployment minimum**; validation with the provisioned
`cryptography>=43` environment is still required. No dependency install or
deployment is implied by these local tests.

Before deployment, provision and independently verify the exact KMS ARN/public
key pair, confirm the existing verified seed/version, preserve state and service,
and review the merged configuration. Separately authorized follow-up must cover
Pi dependency/runtime compatibility, PTY transport tests, actual KMS RAW signatures,
version-specific S3 download, MQTT ingestion, UART state freshness/button behavior,
and a supervised physical install. None is claimed by software emulation.