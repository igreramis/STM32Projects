# Pi AWS IoT lab client

This directory is the version-controlled gateway source alongside the STM32
firmware. It was imported from the existing external pi-client development
directory; changes here are not automatically synchronized to that directory
or to the Pi. Deployed copy belongs in `/home/pi/aws-iot`; dependencies are in
its existing `.venv`. No systemd service is installed. Never put private keys
into source control.

## Current OTA support and configuration

See [the cloud install guide](../../docs/CLOUD-INSTALL.md) for the explicit
`--ota-config` installer, manual BOOT0/B2 sequence, durable results and safety
limits. Configuration and service examples are not enabled deployments.
The staging/read-only behavior documented below describes the default mode;
opt-in OTA adds a separate `install_firmware` command and durable job journal.

Copy `config.example.json` to local-only `config.json`, replace the placeholder
endpoint, and provision certificates outside source control. `release.json`
is required at gateway startup and by the staging tests: it contains the
approved original S3 object's identity/hash, not credentials or a signed URL.
Preserve this approval separately from the rebuilt local 0.1.0 OTA image.
Private configuration, keys, signed commands, environments and run backups are
ignored. Approved release binaries/catalog/baseline must be provisioned locally
as described in the guide; they are not included in this source snapshot.

Full software regression suite (no hardware or network operations):

```sh
python -m unittest -q test_rom_program test_rom_read_id test_rom_local test_heartbeat_monitor test_client test_firmware test_ota_installer test_ota_jobs test_client_ota
```

Install both requirements files into the chosen environment before testing.

## Run on the Pi

```sh
cd ~/aws-iot
.venv/bin/python -m unittest -v test_client
.venv/bin/python client.py --duration 30
# Continuous foreground run; Ctrl+C stops it:
.venv/bin/python client.py
```

Configuration is loaded from `config.json` beside the script. Paths containing
`~` refer to the Pi user's home. This client uses MQTT 3.1.1 over verified mutual
TLS on port 8883, client ID `pi-lab-01`, using the existing scoped IoT policy.
Only run one instance with this client ID; otherwise AWS disconnects the other.

## Console test

In the Sydney AWS IoT MQTT test client, subscribe to:

- `lab/pi-lab-01/telemetry`
- `lab/pi-lab-01/acks`

Publish to `lab/pi-lab-01/commands` with retain OFF:

```json
{"command_id":"test-001","command":"get_status"}
```

Expect an acknowledgement with matching `command_id`, `status: "ok"`, UTC
timestamp, Linux host uptime and the number of telemetry messages whose PUBACK
has been received during this process. Telemetry defaults to every 10 seconds.
Use a new command ID for a fresh snapshot. Repeating an ID returns the cached
response while it remains in the process-local 128-entry cache.

Malformed commands are logged and ignored; valid IDs with unsupported commands
get an error acknowledgement. There is no shell, reboot, PWM or arbitrary
execution command. Retained deliveries are ignored; do not publish retained
commands even to an already connected client (MQTT 3 retain delivery semantics).

## Reconnection and limits

The SDK reconnects established sessions with 1–30 second backoff. Subscriptions
are renewed after a session loss. While offline, telemetry is skipped rather
than accumulated. The received-command queue is capped at 32 and locally queued
messages older than 60 seconds are dropped. Message payloads are capped at 4 KiB.
Only one publish is outstanding; a publish timeout exits with an error instead
of building an offline backlog. Initial connection/subscription errors also
exit. No automatic process restart or durable offline storage is implemented.
QoS 1 may duplicate delivery; IDs make the read-only command replay-safe within
the bounded cache. The cache and telemetry counter reset on restart.

## Verification scope

Unit tests use mocked network callbacks and do not prove AWS authorization or
real network reconnection. A successful MQTT connection, subscription and PUBACK
prove only those operations. Console receipt and the get_status round trip must
be checked separately. Keep the observer's permissions separate from the device
policy; do not broaden the Pi policy simply to observe its own publications.

For the later network-loss test, don't disable Wi-Fi over the only SSH link
without a recovery plan. Prefer a bounded foreground run with local access, or
test after supervised startup is installed. Do not erase the original SD backup.

## Firmware staging (download only)

`stage_firmware` downloads **only** the locally approved release in `release.json`:
NUCLEO-G0B1RE, version 0.1.0, 19,212 bytes, the pinned SHA-256 and exact S3 version
`vtuJfI9yuJtFx6EqWSkSpULFENCCgo54`. This local approval file is separate from the
already-uploaded manifest; no need to overwrite either S3 object. Changing an
approved release requires an operator to update the local approval file.

Use `stage-command.template.json` in the existing commands topic, retain OFF.
Replace its URL placeholder with a short-lived **version-specific** presigned
GET URL. Required URL host:
`iot-lab-firmware-844274146091.s3.ap-southeast-2.amazonaws.com`.
The URL must contain the matching `versionId` query parameter and an AWS SigV4
signature. Select the object's specific version before requesting a URL.
**Do not append versionId after signing**; this invalidates the signature.
The signing console identity needs permission to read this object version
(`s3:GetObjectVersion`). Pi IoT permissions need no changes; the presigned URL
authorizes HTTPS download without permanent AWS access keys on the Pi.

Use the template as follows (replace the placeholder locally, not in chat):

```json
{
	"command_id": "stage-001",
	"command": "stage_firmware",
	"version": "0.1.0",
	"url": "PASTE_VERSION_SPECIFIC_PRESIGNED_HTTPS_URL_HERE"
}
```

The acknowledgements topic receives `status: "downloading"` followed by
`status: "verified"` or `status: "error"`. A verified response contains
`flashed: false`, size, SHA-256, S3 version and staging path. `get_status` includes
the latest attempt under `data.firmware_stage`. There is **no GPIO, UART, reset,
flash, install or reboot operation**. SHA-256 is an integrity check, not a
firmware publisher signature. The local approval file supplies expected values;
the client does not blindly trust a manifest supplied in an MQTT command.

HTTPS validates system CA roots, with proxies and redirects disabled. Only the
configured host/path/version are accepted. Response version, Content-Length,
actual size and SHA-256 must all match. A private temporary file becomes
`~/aws-iot/staged/firmware.bin` only after verification via atomic replacement.
A failed transfer leaves the previous staged binary intact and removes its
partial download. There is one staging slot, at most one active download, a
512 KiB configuration ceiling, 5-second socket timeout and a 30-second elapsed
deadline checked between reads. A blocked I/O call can exceed the elapsed
deadline by its timeout; OS DNS resolution is not bounded by this check.

This minimal lab client performs download on its application thread, **not**
the SDK callback thread. Telemetry and application command processing pause
during the transfer; MQTT network callbacks continue and the inbox remains
bounded. Shutdown waits for the transfer to finish or fail. Intermediate MQTT
status is not durably queued, and an interrupted publish can still exit the
client. Restart does not automatically report or resume an earlier attempt.
There is no crash/power-loss durability guarantee or anti-rollback enforcement.

Duplicates reuse the cached final response within this process (128 entries).
Changing request fields under the same command ID returns `command_id_reused`.
Use a new command ID and fresh signed URL to retry. URLs are not stored in
status, files or the replay cache and are not deliberately logged; they exist
transiently in memory and in the AWS console. Treat them as temporary secrets.

Run all tests with `.venv/bin/python -m unittest -v test_client test_firmware`.
Downloader tests use synthetic bytes and mocked HTTP responses. They do not
prove S3 permission, TLS connectivity or real delivery; those require the
separate live test using your private S3 object.