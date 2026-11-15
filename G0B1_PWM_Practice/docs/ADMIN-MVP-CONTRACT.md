# Admin MVP implementation contract

## Scope and safety

- One authenticated admin, existing `pi-lab-01` gateway and `stm32g0b1-01` subdevice.
- Dashboard and device details, firmware upload/approval/deploy/history. Onboarding and Telemetry navigation visible but disabled.
- Online status uses fresh observations, not merely a gateway connection. Operating state is separate from connectivity.
- Keep `pi-iot-ota.service`. All manual boot/reset instructions remain exclusively in Pi journald, never in the web UI/API responses.
- No physical flashing, reset, option writes, or button verification during software implementation. Hardware tests are explicitly deferred.
- Existing 512 KiB backup, known predecessor, 20 KiB erase region, vector checks, preserved tail and full readback remain mandatory. No automatic retry/rollback.

## Signed release protocol (v1)

The backend signs UTF-8 JSON serialized with sorted keys and compact separators. Payload fields are exactly:
`schema` (1), `target` (`stm32g0b1`), `version` (numeric semver), `size_bytes`, `sha256`, `bucket`, `region`, `key`, `version_id`.

Envelope: `{ "key_id": "<KMS key ARN>", "payload": "<base64 JSON bytes>", "signature": "<base64 signature>" }`.

Signing is AWS KMS RSA_2048 `RSASSA_PKCS1_V1_5_SHA_256`, RAW message. Gateway pins the public PEM and exact key ARN locally; verifies the original payload bytes before parsing, requires canonical form, validates exact fields and S3 location. No network key discovery.

Dynamic install command adds `release` to existing fields:
`command_id`, `command`=`install_firmware`, `version`, `from_version`, `sha256`, `expires_at`, `url`, `release`.
Legacy commands remain supported. New command envelope limit 8192 bytes (bounded). URL must select the signed S3 object version. Commands expire within 30 minutes.

The gateway persists its last full-readback-verified version/hash, seeded only from the existing verified local baseline/catalog. It never trusts the first arbitrary backup. New expected full flash hashes are computed using the signed image, FF erase padding, and the pinned original preserved tail. A mismatched predecessor fails before erase. A crash without persisting a new verified state fails closed on the next attempt.

## Telemetry protocol

Keep legacy exact version heartbeat for compatibility and application verification. Additional UART JSON line (<=512 bytes):
`{"type":"state","device_id":"stm32g0b1-01","firmware":"0.4.0","state":"idle","sequence":0}`.
States: `idle`, `ready`, `running`, `paused`, `fault`; stable button press advances once. Initial state idle on reboot. Send at startup, on change, and every 10 seconds. Sequence is a uint32 sample counter, not a cross-reboot identifier.

Gateway MQTT telemetry retains existing top-level fields, adding `boot_id` and `subdevices` array. Each subdevice has `device_id`, `firmware` (or null), `state` (or null), `sequence` (or null), `last_seen` (ISO UTC or null), `age_seconds` (number or null), `updating` (boolean). `last_seen` advances only on validated UART observations, never on repeated cloud publishing. UART ownership must be handed off synchronously before installation and restored afterwards; continuous monitor is receive-only 8N1. Device stale threshold 35 seconds; gateway 35 seconds.

## HTTP API contract

All `/api/*` routes require Cognito access JWT and membership in `admins`. UI stores tokens only in memory/session storage; authorization code + PKCE, no embedded AWS credentials. Server returns sanitized errors.

- `GET /api/devices` => `{devices:[{device_id,name,kind,parent_id,online,last_seen,firmware,state,updating,children?:[]} ]}`; flat array with gateway and subdevice.
- `GET /api/releases` => `{releases:[{release_id,version,target,size_bytes,sha256,status,created_at}]}`.
- `POST /api/releases` body `{version,filename,content_base64}` => release row. Strict binary size/vector/embedded-version validation, immutable version, private versioned S3 object. Status `uploaded`.
- `POST /api/releases/{release_id}/approve` => release row with status `approved`; backend revalidates the exact S3 version then signs it.
- `GET /api/deployments` => `{deployments:[{command_id,device_id,version,from_version,status,phase,created_at,updated_at,error?,flash_verified?,application_health_verified?}]}`.
- `POST /api/deployments` body `{device_id:"stm32g0b1-01",release_id,request_id:"<UUID>"}` => deployment row. Requires fresh gateway and subdevice, verified current firmware, approved release, no active deployment. Request ID is idempotent. Cloud publishes only once, never silently republishes an ambiguous publish.

Backend MQTT rules consume only `lab/pi-lab-01/telemetry` and `/acks`, validate identity/schema, and apply monotonic per-job `event_seq`. No raw MQTT payload/error text/URLs are exposed to web clients. Cloud deployment status is not hardware proof until the Pi reports it.

Runtime frontend config at `/config.json`: `{apiBaseUrl,cognitoDomain,userPoolClientId,redirectUri,region}`. Demo mode must be explicitly labeled, uses fixtures only, never publishes real deployment commands.

## Deployment

CloudFormation-managed Cognito, HTTP API, Lambda, DynamoDB, KMS signing key, IoT rules, private UI S3 + CloudFront OAC; reuse the existing private firmware bucket. No static website public bucket. Cost-bearing cloud resources are limited to this lab MVP; no NAT/EC2/RDS. Login requires a one-time admin password setup directly in a terminal (never through chat).