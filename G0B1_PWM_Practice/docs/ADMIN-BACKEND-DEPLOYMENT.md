# Backend deployment — 2026-11-16

**Historical record:** current artifact, gateway state and enabled single-use-arm
mode are documented in [the latest rollout](ADMIN-SINGLE-USE-ARM-DEPLOYMENT.md).
The supervised 0.4.0 hardware result is in [the trial](ADMIN-OTA-040-TRIAL.md).

This record includes the final local suite and live checks after HTTPS hosting
and the telemetry clock fix. Historical Pi checks are identified separately.

## Verified live

- Account `844274146091`, region `ap-southeast-2`, profile `ota-admin`.
- Stack `stm32-admin-backend`: `UPDATE_COMPLETE`.
- API: https://ujporks393.execute-api.ap-southeast-2.amazonaws.com
- Cognito pool: `ap-southeast-2_NLOMDdj6F`; client: `158s3iemunrqk62gg1q45a1ch9`.
- Cognito domain: https://admin-api-844274146091-ap-southeast-2.auth.ap-southeast-2.amazoncognito.com
- Table: `stm32-admin-backend-AdminTable-1V2R4FX84ME18`.
- Signing key: `arn:aws:kms:ap-southeast-2:844274146091:key/7ae64c36-9835-4346-bc18-42222602f31e`.
- Separate HTTP/ingestion Python 3.12 ARM64 Lambda functions, JWT authorizer,
  scoped IAM roles, 14-day logs, telemetry/ACK IoT rules, retained table/key.
- Backend artifact is content-addressed under `admin-artifacts/` in the existing
  private versioned firmware bucket; existing firmware objects were not changed.
- Final deployed artifact key:
  `admin-artifacts/backend-50dc0bf2b11fb7a2ebfac5919458c7bba59a6dabdab45953572099e126204c6d.zip`
  in `iot-lab-firmware-844274146091`.
- Frontend stack `stm32-admin-frontend`: https://dw4cy96r64rff.cloudfront.net/;
  bucket `stm32-admin-frontend-assetsbucket-5vbhh8lktsrc`, distribution
  `E357Y9JALJ966S`. See [hosting and rollback](ADMIN-FRONTEND-HOSTING.md).
- Backend `WebOrigin` is exactly `https://dw4cy96r64rff.cloudfront.net` (no slash).
  `CallbackUrl` and `LogoutUrl` are exactly
  `https://dw4cy96r64rff.cloudfront.net/` (with slash). Localhost live authentication
  is no longer registered; explicit local **?demo=1** still works.

## Validation

- **93 backend/adapter/infrastructure tests passed**, including **14 new clock
  tests**; **47 UI tests, build and lint passed** in the final validation run.
- AWS CloudFormation template validation passes; whitespace check passes.
- [Live smoke test](../cloud-admin/smoke_test.py) passed:
  - Unauthenticated OPTIONS preflights succeed with the exact allowed origin/headers.
  - All three public GET routes reject unauthenticated requests with HTTP 401.
  - IAM-authorized Lambda invocations read devices/releases/deployments from DynamoDB.
  - Firmware deployment returns `403 deployment_disabled` before writes/effects.
  - Ingestion rejects invalid events.
  - Cognito discovery issuer matches and KMS key is RSA_2048 SIGN_VERIFY.
- Direct Lambda invocation uses simulated authorizer context; it is not login proof.
- [Hosting smoke](../cloud-admin/smoke_frontend.py) passed HTTPS/assets/hashes,
  runtime config, security/cache headers, missing-file failures and private S3/OAC
  checks. Backend live smoke also passed after the final update.
- Separately observed at the **hosted HTTPS origin**: authenticated
  `GET /api/devices`, `GET /api/releases` and `GET /api/deployments` all returned
  **200**. Logout → Cognito login → return to dashboard was observed; the user
  handled the password. Scopes are `openid profile admin-api/access`, not `email`.
  Both gateway and STM32 were online, STM32 firmware **0.3.0**, **0.4.0 approved**.
- CORS fix: explicit unauthenticated OPTIONS route with an integration, plus an
  empty side-effect-free 204 handler. The ANY data route retains scoped JWT checks.
  A targetless OPTIONS route did not resolve the preflight failure; with a target,
  Lambda also needed to allow OPTIONS before authentication.
- Prior dependency audit: **0 runtime vulnerabilities; 7 moderate development
  lint-chain findings**, involving `minimatch` 10.2.6, **GHSA-g3hv-75wg-m249**.
  Review compatible fixes; do not blindly force a downgrade.

## Transient telemetry rejection and bounded fix

- Numeric-only diagnostics recorded `source_clock` deltas of **−6 to −22 ms**
  (broker receipt minus source timestamp); Pi NTP offset was **−24.796 ms**.
  These are recorded transient measurements, not a current clock reading.
- The tested/deployed fix sets an explicit **100 ms source-clock tolerance only**:
  source delta is accepted from −0.100 through +35 seconds. It does not relax the
  **35-second stale limit** or the broker-receipt gate (still 0 through 35 seconds).
- Timestamp validation, timestamp/count ordering, retired-boot/replay protection,
  and original UART observation timestamps are unchanged. No replacement with
  broker time, synthetic freshness or tolerance for future UART observations.
- After the final artifact update, fresh observations on the hosted dashboard
  advanced every **10 seconds**. Both devices remained online with firmware 0.3.0
  and no update in progress. This is telemetry evidence, not 0.4.0 installation.

## Firmware 0.4.0 upload/signing verified — 2026-11-16

- User previously uploaded and approved 0.4.0 through the authenticated dashboard.
  **No new upload was performed at the hosted HTTPS origin.**
- Built binary: 20,152 bytes (below 20 KiB), SHA-256
  `28c6deeb8f0a8dfdddad68303bfc6505243f6929f97f28d11b56a2bef950a416`.
- S3 key `firmware/stm32g0b1/0.4.0/firmware.bin`, exact version
  `qfhEPtXZJ7m5eUvmreMgBanFwSGJqYQG`; downloaded bytes equal the local build.
- [Read-only verifier](../cloud-admin/verify_approved_release.py) fetched the approved
  envelope and public key from the pinned stack KMS key. The actual gateway
  `ReleaseVerifier.verify()` and `validate_image()` passed locally on this workstation.
- Tampered payload, signature, and image were each rejected. This verifies software
  compatibility, not execution on the Pi or installation on the STM32.
- Exact S3 version/bytes, signature/image validation and tamper rejection were
  reverified after the final backend update; no new release upload was needed.
- No deployment was published and no hardware was accessed.

## Pi recovery and commissioning preparation — 2026-11-16

- After the editor crash, SSH inspection confirmed the original service was still
  active (PID 955); no signed modules or merged configuration had reached the live root.
- Recovered candidate: `/home/pi/aws-iot/deploy-candidates/signed-20261116`.
  Backup: `/home/pi/aws-iot/deployment-backups/signed-20261116/installation.tar.gz`
  (gzip integrity checked, private directory 0700/archive 0600; unit backup matches).
- Restored two omitted test fixtures to the candidate. All 380 offline tests pass
  on Pi Python 3.13 with cryptography 50.0.2, awscrt 0.36.1 and pySerial 3.5;
  four PTY cases remain deferred. All staged gateway source hashes match the workspace.
- Both historical jobs are terminal and fully acknowledged. Saved readback
  `ota-runs/rom-q4clpuyd/flash-after.bin` matches the validated 0.3.0 catalog and
  original baseline tail: full-flash SHA-256
  `d484075d8aee2d858fa3c0d55cf2746fd2c3adfead11654c6a8f9711172bfb87`.
  This is historical readback evidence, not a fresh hardware read.
- Candidate configuration preserves existing paths/download approval, adds the
  pinned public signing key, `seed_version=0.3.0`, `uart_monitor=true`, and
  **`install_enabled=false`**. Cloud deployment disable was also rechecked.
- With copied temporary journals and hardware/network blocked, candidate loading,
  predecessor initialization, no-effect install rejection, and actual KMS 0.4.0
  signature/tamper verification passed on the Pi. Live config/state remain unchanged.
- A guarded activation helper and hash manifest are staged. It requires an inactive
  service, unchanged config/journals/unit and free UART; it takes another consistent
  backup before copying modules/config and initializing the verified predecessor.
- Preparation was completed before the service cutover recorded below.

## Pi commissioning completed — 2026-11-16 local / 2026-11-15 UTC

- Gracefully stopped the idle original service, ran the guarded activation helper,
  and restarted **the same `pi-iot-ota.service`** at 21:08:36 UTC. New PID 1617;
  subsequent checks: active, zero restarts, sole `/dev/ttyAMA0` owner.
- Fresh consistent private backup:
  `/home/pi/aws-iot/deployment-backups/signed-cutover-20261115T210833Z/installation.tar.gz`.
  Its adjacent `cutover-evidence.json` records installed hashes, predecessor pin,
  unchanged service and unchanged original job journals. No rollback was needed.
- The original service unit, MQTT identity/certificates, existing download approval,
  baseline/catalog/images, run backups and two acknowledged job records were preserved.
- Initialized `ota-state/verified-flash/predecessor.json` and its durable marker
  under the coordinator lock from the verified historical 0.3.0 evidence above.
- Active OTA config has **`install_enabled=false`**, **`uart_monitor=true`**,
  pinned KMS public key/ARN and seed 0.3.0. Cloud deployment remains disabled too.
- Real AWS IoT ingestion writes fresh observations with boot ID
  `1e0efd7a3f3c4f5286a247983037c7ca`; telemetry count advanced from 2 to 10.
  At count 10, subdevice firmware was **0.3.0**, UART observation age about four
  seconds, `updating=false`, state/sequence null as expected for legacy firmware.
  These are fresh application heartbeats, not a new full-flash readback.
- Live backend smoke tests still pass (preflight, JWT rejection, runtime reads,
  disabled deployment, invalid ingestion, Cognito and KMS). The IAM-invoked deployed
  devices handler reports both gateway and STM32 online, firmware 0.3.0, no update
  in progress. This invocation is not browser login proof. The browser was signed
  out immediately after the crash; subsequent hosted login/logout and authenticated
  GET verification are recorded above and supersede that temporary state.
- Only the application's receive-only UART monitor was started. **No STM32 reset,
  ROM entry, flash, install command, BOOT0 change or button test was performed.**

## Safety and remaining work

- Cloud **`FIRMWARE_DEPLOYMENT_ENABLED=false`** and gateway
  **`install_enabled=false`**; neither Lambda role has `iot:Publish`.
- Final IAM simulation returned **explicitDeny** for both execution roles on the
  installation-command topic. Pi guard/monitor settings were rechecked read-only;
  the same service remained active, PID 1617, zero restarts.
- No installation command, physical flashing, reset or button verification was
  performed in this work. The gateway software/configuration cutover and frontend/
  backend cloud updates above are complete; **no actual firmware deployment** was
  performed. Do not confuse software hosting with a device installation.
- Hosted authentication, all three GETs, logout/login return, final software checks,
  live smoke and fresh telemetry are completed evidence, not remaining work.
- A new upload at the HTTPS origin is **not verified**; earlier UI upload/approval
  and repeated exact S3/signature/tamper checks are the available evidence.
- Previous Pi evidence remains **380 offline tests passed, four PTY cases deferred**;
  these were not rerun for this update. Physical **0.4.0 installation and operating-
  state/button tests remain deferred until explicit approval**. No hardware or
  install commands were issued for this documentation update.
- Preserve exact hosted CORS/callback/logout settings and current backend artifact
  parameters on future updates. Do not weaken authentication or installation gates.
- No commit or push was performed; previous recorded checkpoint is `5f1aca2`.

## Initial rollback and retained resources

First stack creation failed because the HTTP API lacked a Name. Added an explicit
stack-derived Name, removed the rolled-back stack record, and recreated successfully.
Retention preserved these **unused resources from the failed attempt**:

- Table `stm32-admin-backend-AdminTable-557U0GFCAM8H`.
- KMS key `8821ee31-e8cc-48f1-b827-53001c886025`.

They are not used by the successful stack and were not deleted. Review/clean up
separately to avoid unnecessary ongoing charges. The active table/key above are
different; never confuse the two sets.