# Single-use deployment authorization — deployed 2026-11-16

## Current operating mode

This record supersedes older telemetry-only/default-off deployment descriptions.
The prior supervised 0.4.0 installation and button checks remain recorded in
[the trial record](ADMIN-OTA-040-TRIAL.md). No new firmware installation, physical
reset, ROM entry or arm request was submitted during this software rollout.

Normal workflow now requires no per-update configuration edit, CloudFormation
deployment, service restart or SSH administration:

1. Upload and approve an immutable new firmware version in the dashboard.
2. Select the release, review the device/version, and choose **Arm this deployment**.
3. Choose **Confirm and send** within the displayed 120-second authorization window.
4. Follow the two physical prompts in Pi journald. Download, backup, restricted
   erase/write, full readback, application checks and cloud reporting are automatic.

The arm does not itself install anything. It is bound to one UUID, gateway,
device, predecessor and exact signed release. The backend consumes it atomically
with the job and lease; the Pi checks the signed permit before durable admission.
Replay returns the existing result, not another installation. The admission
deadline does not interrupt an accepted operation during flash programming.
There is no automatic retry, rollback, GPIO drive, reset, mass erase or option write.

## Recovery and verification

The editor crash left code saved but deployment unstarted. Recovery verified the
old backend artifact and disabled gates, Pi PID 2086, four terminal/ACKed jobs,
and the unchanged verified 0.4.0 predecessor before resuming.

- Host backend: **122 tests passed**.
- Host gateway: **403 guarded tests passed; four PTY cases deferred**. The prior
  pre-crash test-created-PTY-only run also passed all 407 cases.
- UI pre-crash checkpoint: **79 tests, production build and lint passed**.
- Pi Python 3.13 candidate: **471 guarded tests passed**, comprising 403 gateway
  cases plus 68 backend fixture-module tests discovered in the staging directory;
  four physical-I/O-blocked PTY cases deferred.
- Initial Pi candidate run missed the cross-component backend test fixture.
  Supplying the matching backend modules fixed staging; no production fix was
  needed for that import error.
- Candidate load on copied existing state rejected a structurally valid unsigned
  request with `invalid_deployment_arm`, with network/UART/worker creation blocked.
- Live disabled-mode smoke checked both arm/deploy rejection before effects.
- Live enabled-mode smoke passed read-only/CORS/auth checks and deliberately
  skipped arm/deploy POSTs. Hosting smoke verified exact assets, HTTPS, private
  S3/OAC, security headers and caching.
- Reloaded authenticated browser loaded the new asset and reported both devices
  online, firmware 0.4.0, idle, not updating. Existing successful and failed jobs
  remained visible. No new physical install through the arm flow is claimed.

## Live deployment evidence

### Backend and UI

- Stack `stm32-admin-backend`: `UPDATE_COMPLETE`.
- Current artifact:
  `admin-artifacts/backend-eb2ce57e27e6843e2b7ff590c54431fade932f3379e494f54fd11ab54dc799fb.zip`.
- Bucket `iot-lab-firmware-844274146091`; artifact object version
  `tN4qiOVqecqVSOhrArnOSFU.9vx9z55I`.
- Cloud `FirmwareDeploymentEnabled=true`: **emergency master enabled**, not an
  active authorization. Only HTTP role has exact gateway-topic publish rights;
  the backend still requires the immutable unused arm for each new deployment.
- Same KMS key, table, Cognito pool/client, API, CORS and callback/logout settings.
  KMS description changed only; the signer was not replaced.
- UI https://dw4cy96r64rff.cloudfront.net/ loads
  `assets/index-CgnHgPQa.js` (SHA-256
  `7c4220fc71dd2096bc388ec6b7711840d67f32aac52e8be46b44330098557730`).
- CloudFront invalidation `IDFIOCR1OCQ37MOFM15W3518UG`.
- Final consistent DynamoDB check: **zero arm records; no gateway lease**.

### Pi

- Same `/home/pi/aws-iot` installation and `pi-iot-ota.service`; active PID 2435,
  zero restarts after deliberate idle cutover. Receive-only UART telemetry resumed.
- `install_enabled=true`, `require_deployment_arm=true`, `uart_monitor=true`.
  Local master enable is not permission to accept unsigned/expired/reused commands.
- Configuration SHA-256:
  `8a948c2f6f94710ed5b7cc5df41ec1ca6c4d093c419be253aebf0715714adbe6`.
- Private consistent backup:
  `/home/pi/aws-iot/deployment-backups/arm-cutover-20261116T003757Z/installation.tar.gz`.
  Adjacent `arm-cutover-evidence.json` records hashes and preservation checks.
- Guarded one-time helper: local `build/cloud-admin/activate_arm_candidate.py`,
  staged at `/home/pi/aws-iot/deploy-candidates/arm-20261116/`. It is not a normal
  update command and refuses reactivation against changed config/state.
- All four terminal jobs and ACK revisions, predecessor, marker, original
  catalog/baseline/images, credentials, MQTT identity and unit were retained.
  Predecessor remains verified 0.4.0, full flash SHA-256
  `f203113cb0b6d16b6cd50a2bd204e508a21f3af2a268492dbb81c1f957a22515`.
- Changed production module hashes matched workstation and staged versions:

| Module | SHA-256 |
| --- | --- |
| deployment_arm.py | afc5885d491c711624a9963259ef0560fc196c9694b8d5f3520abcbcf6635779 |
| ota_jobs.py | 20e5352480d064918928c5733556fd7180f352c9b94d465266227832b077b993 |
| ota_installer.py | 93d63175048a4b1ae439519dd5f7f320b988dfc0271ab0e74777e85f1bf84e2a |
| signed_release.py | 26e08d2a1fbe4dfe228862de1980cfcecfe68059bbc2446c2e5b793b5c937f65 |

## Emergency stop and future operations

The master switches remain emergency admission controls, not per-update chores.
Setting cloud `FirmwareDeploymentEnabled=false` prevents new arm/deploy requests
and removes HTTP publish permission. Preserve the current artifact and origins.
This does not cancel accepted work. **Never stop/reset the Pi or STM32 during
programming.** Local gate/config changes require terminal/ACKed idle state and
the existing guarded backup process. Do not disable permit verification while
leaving installations enabled. Never delete/reseed journals or predecessor pins.

The frontend publisher currently requires cloud disablement during software
publishing; that is a maintenance safeguard, not a firmware-update requirement.
No new commit or push was performed for this rollout; last pushed checkpoint
remains `06fa8b8`.