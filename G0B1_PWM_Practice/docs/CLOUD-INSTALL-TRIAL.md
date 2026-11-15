# Cloud installation deployment and supervised trial

## 2026-11-15 — cloud-triggered local-image trial passed

Job `ota-trial-20261115T034047Z` installed local 0.2.0 over 0.3.0.
User supplied the AWS acknowledgement: application_verified, all verification
flags true, event_seq 14. Subsequent SSH inspection confirmed durable status
application_verified, delivery_seq 14 and acked_seq 14. Full readback completed
03:43:26 UTC with SHA-256
5fd3f36a1f94f07e42709ba1e4976a080caea92425d4730314a2866d80fd718e.
The ROM record's application_health_verified=false describes its ROM-only phase;
the coordinator's later true flag records the fresh heartbeat check.
This trial used the local image, not a new S3 download.

## 2026-11-15 10:25 UTC — fresh S3 trial approval activated

User uploaded the approved 0.3.0 binary to the private versioned bucket and
provided Version ID `loCKnSGw1ch6DSdtgKg._pd09hcByOxq` for
`firmware/stm32g0b1/0.3.0/firmware.bin`.
New [approval metadata](../gateway/pi-client/approved-releases/0.3.0.json)
pins that version, 19536-byte size and SHA-256
f5f2f9c3c24f9f2f3e9931ad78446938454dd303bea4d866ae1b076dfb81ebbb.
Laptop and Pi binaries matched this hash; approval validated against the Pi's
existing complete catalog and original baseline without network/UART access.
The S3 bytes have not yet been independently downloaded/verified.

After confirming all jobs terminal, stopped the idle service gracefully,
backed up its OTA config under
/home/pi/aws-iot/deployment-backups/s3-approval-20261115T102527Z,
installed private approved-releases/0.3.0.json and added only the matching
download_releases mapping to ota-config.json. Preserved state/run roots.
Existing gateway config, original release approval, original staging slot,
catalog and baseline hashes unchanged. Coordinator validation healthy/inactive.
Restarted service PID1189; MQTT connection/subscription and telemetry PUBACK
confirmed at10:25:29 UTC. No flash/download/boot command issued.

Next: generate a short-lived presigned URL for that exact object version in the
user's authenticated console, then publish a unique URL-bearing install job
from0.2.0 to0.3.0 and supervise manual BOOT0/B2 prompts. Do not store signed URLs
in this document or source control. Fresh-S3 trial is not yet complete.

The following deployment entries are historical; activation prerequisites below
have since been satisfied as described above.

## 2026-11-15 — deployment completed; activation pending

User authorized deployment, service enablement and one supervised cloud-to-STM32
installation. No install command has been published or UART opened in this task.

- SSH alias pi reachable; /dev/serial0 resolves to /dev/ttyAMA0.
- No gateway, programmer or heartbeat-monitor process detected; UART unowned.
- Pi Python 3.13.5; pyserial 3.5, awsiotsdk 1.31.0, awscrt 0.36.1 unchanged.
- Candidate: /home/pi/aws-iot/deploy-candidates/ota-20261115T030359Z.
- Initial 309-test candidate run exposed a test-only assumption that /dev/serial0
  was not a symlink. Fixed the expected resolved path and added a deterministic
  symlink test; no application code change. Local coordinator tests: 47 PASS.
- Complete corrected Pi suite: **310 tests PASS in 43.060s**; pip check PASS.
- Existing gateway config, all three release images and original full baseline
  passed offline validation. No dependencies installed or upgraded.
- Source hashes matched repository before installation. Tested code deployed to
  /home/pi/aws-iot; replaced files saved under
  deployment-backups/ota-20261115T030359Z. Per-file copies verified.
- Existing config.json, release.json, staged/firmware.bin, local-releases.json
  and local-baseline.bin hashes unchanged by deployment. Certificates untouched.
- Created private ota-config.json from the reviewed example; journal initialized
  healthy/idle, no worker started. Systemd unit staged as pi-iot-ota.service and
  systemd-analyze verify passed. Not installed/enabled yet.

## Remaining live prerequisites

- Pi sudo requires the user's password; it must be entered directly in the
  terminal, never through chat or a question response.
- Local laptop has no AWS CLI, boto3 or ~/.aws credentials. Use the user's
  authorized AWS console session for publisher/observer operations, or establish
  a separate approved cloud-authentication method without sharing secrets.
- Reconfirm both boards powered, common ground/crossed UART, CN4 caps removed,
  manual target-owned BOOT0 jumper and no competing UART/SWD sessions.
- Confirm current application version with fresh heartbeats before selecting
  the predecessor and target. Earlier user-reported 0.3.0 readback passed;
  its application heartbeat is not yet confirmed.
- Start cloud observation before publishing exactly one unique non-retained
  installation command. Do not claim end-to-end success until expected-version
  heartbeats and the cloud-side final result are observed.