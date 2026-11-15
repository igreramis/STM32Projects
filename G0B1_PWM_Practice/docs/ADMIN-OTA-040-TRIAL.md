# Supervised admin OTA 0.3.0 → 0.4.0 — 2026-11-16

## Final outcome — successful second attempt; both gates disabled

The user renewed readiness and authorized a new supervised request after the URL
fix. Both gates were enabled; corrected artifact remained deployed. Pi backup
`enable-install-20261115T230234Z` preserved the three existing terminal journals
and verified 0.3.0 predecessor. Same service restarted as PID 1949, zero restarts.

New dashboard request: `e5cf168f-a0e6-4f32-9f56-12b61e1a2d6d`, submitted at
2026-11-15 23:04:38 UTC (16 November local). The user completed both physical
prompts from Pi journald. No automatic reset, retry or rollback was performed.

| UTC time | Observed result |
| --- | --- |
| 23:04:39 | Download/image validated; waiting for supervised ROM entry |
| 23:05:35 | Target checks passed: 512 KiB, stored options `0xFEFFFEAA`; full backup started |
| 23:06:31 | Backup matched verified 0.3.0 predecessor; erase intent pages 0–9 |
| 23:06:33 | Erased region verified |
| 23:06:36 | 20,152 bytes written/acknowledged; full readback started |
| 23:07:32 | Full flash verified; outside erased pages unchanged |
| 23:07:56 / 23:08:06 | Two exact 0.4.0 application heartbeats |
| 23:08:06 | Terminal `application_verified`, delivery/ACK sequence 15/15 |

Pi job and consistent DynamoDB job read both confirmed `flash_verified=true`,
`application_health_verified=true`, terminal `application_verified`. Cloud lease
was absent. Dashboard independently displayed flash/application verification.
`flash_may_be_modified=true` records that this job crossed the erase boundary;
it does not negate the later successful verification flags.

### Preserved binary evidence

Pi run directory: `/home/pi/aws-iot/ota-runs/rom-8lgj7lss`.

- Full before/after snapshots: 524,288 bytes each.
- Before SHA-256: `d484075d8aee2d858fa3c0d55cf2746fd2c3adfead11654c6a8f9711172bfb87`.
- After SHA-256: `f203113cb0b6d16b6cd50a2bd204e508a21f3af2a268492dbb81c1f957a22515`.
- Download SHA-256: `28c6deeb8f0a8dfdddad68303bfc6505243f6929f97f28d11b56a2bef950a416`.
- Independent `cmp` from offset 20,480 to end confirmed byte-identical preserved flash.
- Verified predecessor is now 0.4.0 with the full after hash above. Never reseed it.
- Engine events end at flash verification; subsequent application success is
  established by journald, the terminal job and cloud record, not the earlier
  engine event's `application_health_verified=false` field.

### Gates restored and service preserved

- Cloud stack `UPDATE_COMPLETE`, `FirmwareDeploymentEnabled=false`; corrected
  artifact below retained. Disabled branch removes HTTP command-topic publish.
- Local `install_enabled=false`, receive-only `uart_monitor=true`.
- Consistent disable backup:
  `/home/pi/aws-iot/deployment-backups/disable-install-20261115T231019Z/installation.tar.gz`.
- Adjacent gate evidence verified all four terminal/ACKed journals, predecessor,
  initialization marker and systemd unit unchanged by the gate change.
- Same `pi-iot-ota.service` restarted only after job completion: PID 2086,
  active, zero restarts. Fresh 0.4.0 telemetry resumed; no active update.
- Final live backend smoke passed: exact CORS/Cognito, unauthenticated 401,
  runtime reads, disabled deployment rejected before effects, invalid ingest
  rejection and KMS discovery.

### Physical B1 / dashboard checks

All observed with firmware 0.4.0, online, not updating. Times are 16 November
local dashboard observation times; each action was confirmed by the operator.

| State | Last observation | Action |
| --- | --- | --- |
| idle | 10:10:26 | Initial state after service restart |
| ready | 10:11:13 | One B1 press/release |
| running | 10:11:39 | One B1 press/release |
| paused | 10:12:48 | Three-second B1 hold, then release |
| fault | 10:13:13 | One B1 press/release |
| idle | 10:13:30 | One B1 press/release |

The hold test ended at the expected single-step state, consistent with no
hold-repeat; this was not an instrumented debounce/timing measurement. These are
demonstration operating states, not proof of PWM or physical fault detection.
Final state: idle. No new commit or push performed.

## First attempt — safe pre-flash failure; no automatic retry

- Cloud enablement completed; deployed flag was true and actual HTTP role policy
  allowed only the exact gateway command topic. Single-topic IAM simulation with
  actual context returned allowed. A multi-resource simulation returned a generic
  placeholder instead; it was not used as evidence of actual topic permissions.
- One authenticated dashboard request was submitted at 22:39:41 UTC:
  `d28543b0-6d9e-4aef-b61c-7d0f5f5c8fc1`, 0.3.0 → approved 0.4.0.
- Pi stopped at `accepted`, `image_validation_failed`, terminal sequence 2,
  acknowledged sequence 2, `flashed=false`, `flash_may_be_modified=false`.
  No ROM prompt, erase or write occurred. Cloud recorded the same terminal failure
  and released the lease. No second request was submitted until the user renewed
  readiness after the compatibility fix; that authorized attempt is recorded above.
- Both gates restored false; HTTP publish permission removed by the disabled
  template branch. Pi disable backup:
  `/home/pi/aws-iot/deployment-backups/disable-install-20261115T224116Z/installation.tar.gz`.
  Three journals terminal/acknowledged; predecessor/marker unchanged. Same service
  active, PID 1890, zero restarts; firmware remains 0.3.0.

## Compatibility correction

Offline real-SDK reproduction with dummy credentials and blocked networking found
that the previous boto3 S3 construction generates a global S3 hostname rejected
by the gateway's exact regional host policy. Independent signature/image checks
do not test this generated command URL. The historical URL was not retained, so
this is a proven compatibility defect, not definitive reconstruction of that URL.

The backend now explicitly configures the regional S3 endpoint, regional client,
virtual-hosted addressing and SigV4. The Pi URL allowlist is unchanged. Actual SDK
regressions demonstrate old-config rejection and new-config acceptance through
the gateway validator; cloud-to-gateway command tests confirm fields are retained.

- 100 cloud tests and two new gateway admin-deploy integration tests passed.
- Earlier diagnostic run: 382 gateway tests passed, four UART/PTY deferred.
- Fix deployed with installation disabled; live backend smoke passed.
- Current artifact:
  `admin-artifacts/backend-59d25e9ace95582ef064b0c1832c69649081519d6778a9e5ab59bfa0a76650dd.zip`.

The separately authorized second request above verified the corrected download
path on hardware and completed installation. Never reuse either terminal UUID.

## First-attempt authorization and preflight history

The user explicitly authorized one supervised installation and confirmed physical
readiness. Manual boot/reset prompts remain in Pi journald only. No automatic
reset, option-byte change, mass erase, or automatic retry/rollback is authorized.

- Approved 0.4.0: 20,152 bytes; SHA-256
  `28c6deeb8f0a8dfdddad68303bfc6505243f6929f97f28d11b56a2bef950a416`.
- Exact S3 version `qfhEPtXZJ7m5eUvmreMgBanFwSGJqYQG`; signature, bytes and
  tamper rejection reverified before enablement.
- Pi original catalog/baseline validation passed. Two historical jobs are terminal
  and fully acknowledged. Existing verified predecessor remains 0.3.0, full-flash
  hash `d484075d8aee2d858fa3c0d55cf2746fd2c3adfead11654c6a8f9711172bfb87`.
- Gate helper changed only `install_enabled` to true with the idle service stopped.
  Consistent backup: `/home/pi/aws-iot/deployment-backups/enable-install-20261115T223636Z/installation.tar.gz`.
  Adjacent evidence confirms unchanged journals, predecessor/marker and service unit.
- Same `pi-iot-ota.service` restarted successfully: PID 1825, zero restarts,
  sole UART owner, MQTT subscription restored, fresh 0.3.0 observations online.
- Backend template adds default-off `FirmwareDeploymentEnabled`. Enabled branch
  grants only HTTP-role `iot:Publish` on the exact gateway commands topic. Ingest
  role is unchanged. 98 offline tests passed. Smoke checks skip deployment POST
  whenever enablement is true or uncertain.
- Reviewed cloud change set `awscli-cloudformation-package-deploy-1794782207`:
  HTTP role/environment plus dependent integration, no replacements; same artifact,
  Cognito/CORS and all other parameters. It completed before the first request.

## IAM simulator correction

The initial organization denial was not established as a live restriction.
Supplying actual `aws:RequestedRegion`, `aws:PrincipalArn`, and
`aws:SecureTransport` context made the proposed exact-topic permission evaluate
allowed, including Organizations. No organization policy was changed. SCP-only
missing context is not exposed in `MissingContextValues`; do not diagnose policy
restrictions from the incomplete simulation.

## Completion boundary

The supervised installation, binary verification, application startup, cloud
reconciliation, gate restoration and five-state physical check are complete.
Future firmware installations require separate authorization and readiness;
there is no automatic retry, rollback, reset or unattended-install claim.