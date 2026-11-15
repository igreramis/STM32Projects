"""Admin MVP. All external effects are injected; importing this module is offline."""
import base64
import binascii
import hashlib
import json
import logging
import math
import os
import re
import struct
import time
import uuid
from datetime import datetime, timezone

LOGGER = logging.getLogger(__name__)

GATEWAY = "pi-lab-01"
DEVICE = "stm32g0b1-01"
TARGET = "stm32g0b1"
PREFIX = "firmware/stm32g0b1/"
SOURCE_CLOCK_SKEW_SECONDS = 0.1
SEMVER = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\Z")
STATES = {"idle", "ready", "running", "paused", "fault"}
PHASES = ("accepted", "preparing", "downloading", "image_validated", "waiting_for_rom",
          "validating_target", "backing_up", "backup_saved", "erase_started", "programming",
          "verifying_flash", "flash_verified", "waiting_for_application", "application_verified")
TERMINAL = {"application_verified", "error", "interrupted", "rejected"}
ERRORS = {"bootloader_timeout", "bootloader_sync_failed", "target_validation_failed",
          "backup_failed", "programming_failed", "verification_failed", "application_start_timeout",
          "cancelled", "command_expired", "image_validation_failed", "download_not_approved",
          "download_failed", "download_validation_failed", "download_deadline_exceeded",
          "internal_error", "interrupted", "installer_busy", "storage_error"}
RELEASE_FIELDS = ("release_id", "version", "target", "size_bytes", "sha256", "status", "created_at")
JOB_FIELDS = ("command_id", "device_id", "version", "from_version", "status", "phase",
              "created_at", "updated_at", "error", "flash_verified", "application_health_verified")


class Fault(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class Conflict(Exception):
    """A conditional write lost a race. No effects should be retried blindly."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise Fault("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise Fault("invalid_timestamp") from None


def integer(value, maximum=2**32 - 1):
    return type(value) is int and 0 <= value <= maximum


def version(value):
    return isinstance(value, str) and SEMVER.fullmatch(value) is not None


def projection(row, fields):
    return {key: row[key] for key in fields if key in row}


def binary(data, expected_version):
    if not version(expected_version) or not 8 <= len(data) <= 20 * 1024:
        raise Fault("invalid_firmware")
    sp, reset = struct.unpack_from("<II", data)
    if not 0x20000000 < sp <= 0x20024000 or sp % 8:
        raise Fault("invalid_vectors")
    if not reset & 1 or not 0x08000008 <= (reset & ~1) < 0x08000000 + len(data):
        raise Fault("invalid_vectors")
    # Token boundaries matter: 0.1.0 must not accept 0.1.01 or 0.1.0-evil.
    tokens = re.findall(rb"firmware=([^\x00\s]+)", data)
    if not tokens or any(token != expected_version.encode("ascii") for token in tokens):
        raise Fault("firmware_version_mismatch")
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Fault("invalid_json")
        result[key] = value
    return result


class Admin:
    def __init__(self, store, s3, kms, iot, bucket, region, signing_key, issuer, client_id,
                 clock=time.time):
        self.store, self.s3, self.kms, self.iot = store, s3, kms, iot
        self.bucket, self.region, self.signing_key = bucket, region, signing_key
        self.issuer, self.client_id, self.clock = issuer, client_id, clock

    def authenticate(self, event):
        # Signature/expiry verification is performed by the mandatory API Gateway JWT
        # authorizer. Never decode or trust an Authorization header ourselves.
        claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
        if (claims.get("token_use") != "access" or claims.get("iss") != self.issuer or
                claims.get("client_id") != self.client_id or
                "admin-api/access" not in str(claims.get("scope", "")).split()):
            raise Fault("unauthorized", 401)
        groups = claims.get("cognito:groups", [])
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except ValueError:
                groups = groups.strip("[]").split(",")
        if not isinstance(groups, list) or "admins" not in groups:
            raise Fault("forbidden", 403)

    def body(self, event, fields):
        raw = event.get("body", "")
        if not isinstance(raw, str) or len(raw) > 40000 or event.get("isBase64Encoded"):
            raise Fault("invalid_body")
        try:
            body = json.loads(raw, object_pairs_hook=unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(Fault("invalid_json")))
        except (ValueError, RecursionError):
            raise Fault("invalid_json") from None
        if type(body) is not dict or set(body) != set(fields):
            raise Fault("invalid_body")
        return body

    def http(self, event):
        try:
            # Preflight carries no bearer token and must never read or mutate data.
            # Origin/method/header policy is enforced by API Gateway CORS settings.
            if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
                return {"statusCode": 204, "headers": {"cache-control": "no-store"}, "body": ""}
            self.authenticate(event)
            method = event["requestContext"]["http"]["method"]
            path = event.get("rawPath", "")
            status = 200
            if method == "GET" and path == "/api/devices":
                result = {"devices": self.devices()}
            elif method == "GET" and path == "/api/releases":
                result = {"releases": [projection(r, RELEASE_FIELDS) for r in self.store.list("release")
                                       if r["status"] in {"uploaded", "approved"}]}
            elif method == "POST" and path == "/api/releases":
                result = self.upload(self.body(event, ("version", "filename", "content_base64")))
                status = 201
            elif method == "POST" and re.fullmatch(r"/api/releases/[^/]+/approve", path):
                result = self.approve(path.split("/")[3])
            elif method == "GET" and path == "/api/deployments":
                result = {"deployments": [projection(r, JOB_FIELDS) for r in self.store.list("job")]}
            elif method == "POST" and path == "/api/deployments":
                if os.environ.get("FIRMWARE_DEPLOYMENT_ENABLED") == "false":
                    raise Fault("deployment_disabled", 403)
                result = self.deploy(self.body(event, ("device_id", "release_id", "request_id")))
            else:
                raise Fault("not_found", 404)
        except Fault as exc:
            status, result = exc.status, {"error": exc.code}
        except Conflict:
            status, result = 409, {"error": "conflict"}
        except Exception:
            # No traceback, service exception, request, signed URL, or MQTT body is logged.
            status, result = 503, {"error": "service_unavailable"}
        return {"statusCode": status, "headers": {"content-type": "application/json",
                "cache-control": "no-store"}, "body": canonical(result).decode()}

    def upload(self, body):
        v, encoded = body["version"], body["content_base64"]
        if (not version(v) or not isinstance(body["filename"], str) or
                re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\.bin", body["filename"]) is None or
                not isinstance(encoded, str) or len(encoded) > 27308):
            raise Fault("invalid_upload")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise Fault("invalid_upload") from None
        digest = binary(data, v)
        row = dict(release_id=v, version=v, target=TARGET, size_bytes=len(data), sha256=digest,
                   status="reserved", created_at=iso(self.clock()), key=PREFIX + v + "/firmware.bin")
        try:
            self.store.commit([("release", v, None, row)])
        except Conflict:
            raise Fault("duplicate_version", 409) from None
        # A failed/uncertain upload burns the version; never overwrite or silently retry.
        stored = self.store.get("release", v)
        response = self.s3.put_object(Bucket=self.bucket, Key=row["key"], Body=data,
                                      ContentType="application/octet-stream", IfNoneMatch="*")
        object_version = response.get("VersionId")
        if not object_version or object_version == "null":
            raise Fault("bucket_versioning_required", 503)
        row.update(status="uploaded", version_id=object_version)
        self.store.commit([("release", v, stored, row)])
        return projection(row, RELEASE_FIELDS)

    def approve(self, release_id):
        row = self.store.get("release", release_id)
        if not row or row["status"] not in {"uploaded", "approved"}:
            raise Fault("release_not_found", 404)
        if row["status"] == "approved":
            return projection(row, RELEASE_FIELDS)
        obj = self.s3.get_object(Bucket=self.bucket, Key=row["key"], VersionId=row["version_id"])
        stream = obj["Body"]
        try:
            data = stream.read(20 * 1024 + 1)
        finally:
            stream.close()
        if (obj.get("VersionId") != row["version_id"] or len(data) != row["size_bytes"] or
                binary(data, row["version"]) != row["sha256"]):
            raise Fault("release_integrity_failed", 409)
        payload = canonical(dict(schema=1, target=TARGET, version=row["version"],
                                 size_bytes=row["size_bytes"], sha256=row["sha256"], bucket=self.bucket,
                                 region=self.region, key=row["key"], version_id=row["version_id"]))
        signed = self.kms.sign(KeyId=self.signing_key, Message=payload, MessageType="RAW",
                               SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256")
        if len(signed["Signature"]) != 256:
            raise Fault("signing_failed", 503)
        updated = dict(row, status="approved", release={"key_id": self.signing_key,
                       "payload": base64.b64encode(payload).decode("ascii"),
                       "signature": base64.b64encode(signed["Signature"]).decode("ascii")})
        self.store.commit([("release", release_id, row, updated)])
        return projection(updated, RELEASE_FIELDS)

    def devices(self):
        observation = self.store.get("observation", GATEWAY) or {}
        now = self.clock()
        gateway_seen = observation.get("seen")
        sub = observation.get("sub", {})
        seen = sub.get("seen")
        gateway_online = gateway_seen is not None and 0 <= now - gateway_seen <= 35
        return [dict(device_id=GATEWAY, name=GATEWAY, kind="gateway", parent_id=None,
                     online=gateway_online, last_seen=iso(gateway_seen) if gateway_seen is not None else None,
                     firmware=None, state=None, updating=False),
                dict(device_id=DEVICE, name=DEVICE, kind="subdevice", parent_id=GATEWAY,
                     online=bool(gateway_online and seen is not None and 0 <= now - seen <= 35),
                     last_seen=iso(seen) if seen is not None else None,
                     firmware=sub.get("firmware"), state=sub.get("state"), updating=sub.get("updating", False))]

    def deploy(self, body):
        try:
            request = str(uuid.UUID(body["request_id"]))
        except (ValueError, TypeError, AttributeError):
            raise Fault("invalid_request_id") from None
        if body["request_id"] != request or body["device_id"] != DEVICE or not version(body["release_id"]):
            raise Fault("invalid_deployment")
        existing = self.store.get("job", request)
        if existing:
            if existing["release_id"] != body["release_id"] or existing["device_id"] != body["device_id"]:
                raise Fault("request_id_reused", 409)
            return projection(existing, JOB_FIELDS)
        release = self.store.get("release", body["release_id"])
        if not release or release["status"] != "approved":
            raise Fault("release_not_approved", 409)
        observation = self.store.get("observation", GATEWAY)
        sub = (observation or {}).get("sub", {})
        now = self.clock()
        if (not observation or not 0 <= now - observation["seen"] <= 35 or
                sub.get("seen") is None or not 0 <= now - sub["seen"] <= 35 or
                sub.get("updating") or not version(sub.get("firmware"))):
            raise Fault("device_not_ready", 409)
        if sub["firmware"] == release["version"]:
            raise Fault("same_version", 409)
        row = dict(command_id=request, device_id=DEVICE, gateway_id=GATEWAY,
                   release_id=release["release_id"], version=release["version"], from_version=sub["firmware"],
                   status="error", phase="dispatch_unknown", error="dispatch_unknown",
                   created_at=iso(now), updated_at=iso(now), event_seq=0,
                   flash_verified=False, application_health_verified=False, terminal=False)
        # Pessimistic durable intent: even a crash just after this transaction holds
        # the lease forever until a matching terminal ACK or explicit operator investigation.
        try:
            self.store.commit([("job", request, None, row),
                               ("lease", GATEWAY, None, {"command_id": request}),
                               ("observation", GATEWAY, observation, observation),
                               ("release", release["release_id"], release, release)])
        except Conflict:
            existing = self.store.get("job", request)
            if existing and existing["release_id"] == body["release_id"]:
                return projection(existing, JOB_FIELDS)
            raise Fault("deployment_conflict", 409) from None
        try:
            url = self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket,
                "Key": release["key"], "VersionId": release["version_id"]}, ExpiresIn=900)
            command = canonical(dict(command_id=request, command="install_firmware", version=release["version"],
                from_version=sub["firmware"], sha256=release["sha256"], expires_at=int(now) + 900,
                url=url, release=release["release"]))
            if len(command) > 8192:
                self.finish_without_publish(request, "command_too_large")
                return projection(self.store.get("job", request), JOB_FIELDS)
        except Exception:
            self.finish_without_publish(request, "dispatch_preparation_failed")
            return projection(self.store.get("job", request), JOB_FIELDS)
        # Exactly one SDK attempt: AWS adapter disables automatic publish retries.
        try:
            self.iot.publish(topic="lab/pi-lab-01/commands", qos=1, payload=command)
        except Exception:
            return projection(self.store.get("job", request), JOB_FIELDS)
        current = self.store.get("job", request)
        if current["event_seq"] == 0:
            updated = dict(current, status="dispatched", phase="dispatched", updated_at=iso(self.clock()))
            updated.pop("error", None)
            try:
                self.store.commit([("job", request, current, updated)])
            except Conflict:
                pass  # ACK raced the publish response. Its state wins.
        return projection(self.store.get("job", request), JOB_FIELDS)

    def finish_without_publish(self, request, code):
        row = self.store.get("job", request)
        lease = self.store.get("lease", GATEWAY)
        if lease and lease["command_id"] == request:
            self.store.commit([("job", request, row, dict(row, error=code, phase="error", terminal=True)),
                               ("lease", GATEWAY, lease, None)])

    def ingest(self, event):
        try:
            if len(canonical(event)) > 8192:
                raise Fault("invalid_event")
            kind = event.get("ingest_type")
            suffix = {"telemetry": "telemetry", "ack": "acks"}.get(kind)
            if (not suffix or event.get("ingest_topic") != "lab/pi-lab-01/" + suffix or
                    event.get("ingest_client_id") != GATEWAY or event.get("device_id") != GATEWAY):
                raise Fault("invalid_identity")
            received = event.get("ingest_received_at")
            if not integer(received, 2**53):
                raise Fault("stale_event")
            broker_delta = self.clock() - received / 1000
            if not 0 <= broker_delta <= 35:
                # Fixed codes and validated numeric deltas only; never log the event.
                LOGGER.warning("ingest_rejected code=broker_clock delta_seconds=%s", broker_delta)
                raise Fault("stale_event")
            if kind == "telemetry":
                self.telemetry(event, received / 1000)
            else:
                self.ack(event)
            return {"ok": True}
        except (Fault, Conflict, ValueError, TypeError, KeyError, OverflowError):
            return {"ok": False, "error": "event_rejected"}
        except Exception:
            # Trigger AWS async retries without carrying any untrusted content.
            raise RuntimeError("ingest_unavailable") from None

    def telemetry(self, event, received):
        source = timestamp(event.get("timestamp"))
        boot = event.get("boot_id")
        count = event.get("telemetry_count")
        if (not isinstance(boot, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", boot) or
                not integer(count, 2**53)):
            raise Fault("invalid_telemetry")
        source_delta = received - source
        if not -SOURCE_CLOCK_SKEW_SECONDS <= source_delta <= 35:
            LOGGER.warning("ingest_rejected code=source_clock delta_seconds=%s", source_delta)
            raise Fault("invalid_telemetry")
        subs = event.get("subdevices")
        if not isinstance(subs, list) or len(subs) != 1 or not isinstance(subs[0], dict):
            raise Fault("invalid_telemetry")
        sample = subs[0]
        if (set(sample) != {"device_id", "firmware", "state", "sequence", "last_seen", "age_seconds", "updating"}
                or sample["device_id"] != DEVICE or type(sample["updating"]) is not bool):
            raise Fault("invalid_telemetry")
        old = self.store.get("observation", GATEWAY)
        if old and (source <= old["seen"] or (boot == old["boot_id"] and count <= old["count"])):
            return
        changed_boot = not old or old["boot_id"] != boot
        boot_record = self.store.get("boot", boot) if changed_boot else None
        if changed_boot and boot_record:
            return  # Permanently retired boot IDs cannot become current again.
        sub = {} if changed_boot else dict(old.get("sub", {}))
        age, last = sample["age_seconds"], sample["last_seen"]
        if last is None:
            if any(sample[key] is not None for key in ("firmware", "state", "sequence", "age_seconds")):
                raise Fault("invalid_observation")
        else:
            observed = timestamp(last)
            state_valid = ((sample["state"] is None and sample["sequence"] is None) or
                           (sample["state"] in STATES and integer(sample["sequence"])))
            if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= 2**31:
                raise Fault("invalid_observation")
            observation_delta = source - observed
            if not 0 <= observation_delta:
                LOGGER.warning("ingest_rejected code=observation_future delta_seconds=%s", observation_delta)
                raise Fault("invalid_observation")
            age_delta = observation_delta - age
            if abs(age_delta) > 3:
                LOGGER.warning("ingest_rejected code=observation_age delta_seconds=%s", age_delta)
                raise Fault("invalid_observation")
            if not version(sample["firmware"]) or not state_valid:
                raise Fault("invalid_observation")
            # The UART observation time, never the MQTT publication, drives last_seen.
            if sub.get("seen") is None or observed > sub["seen"]:
                sub.update(seen=observed, firmware=sample["firmware"], state=sample["state"],
                           sequence=sample["sequence"])
            elif (observed == sub["seen"] and sample["firmware"] == sub.get("firmware") and
                  sample["state"] is None):
                # The monitor can expire operating state on a repeated snapshot.
                # Clear that evidence without fabricating a new UART observation.
                sub.update(state=None, sequence=None)
        sub["updating"] = sample["updating"]
        updated = {"seen": source, "boot_id": boot, "count": count, "sub": sub}
        changes = [("observation", GATEWAY, old, updated)]
        if changed_boot:
            changes.append(("boot", boot, None, {"first_seen": source}))
        self.store.commit(changes)

    def ack(self, event):
        request, seq = event.get("command_id"), event.get("event_seq")
        if not isinstance(request, str) or len(request) > 64 or not integer(seq, 2**53) or seq == 0:
            raise Fault("invalid_ack")
        old = self.store.get("job", request)
        if (not old or old["gateway_id"] != event["device_id"] or old["version"] != event.get("version") or
                seq <= old["event_seq"] or old["terminal"]):
            return
        status, phase = event.get("status"), event.get("phase")
        if status not in set(PHASES) | TERMINAL | {"running"} or phase not in set(PHASES) | {"error", "rejected"}:
            raise Fault("invalid_ack")
        flags = ("flash_verified", "application_health_verified")
        if any(type(event.get(key)) is not bool for key in flags):
            raise Fault("invalid_ack")
        if event["application_health_verified"] and not event["flash_verified"]:
            raise Fault("invalid_ack")
        if status == "application_verified" and not all(event[key] for key in flags):
            raise Fault("invalid_ack")
        if old["phase"] in PHASES and phase in PHASES and PHASES.index(phase) < PHASES.index(old["phase"]):
            raise Fault("invalid_ack")
        updated = dict(old, status=status, phase=phase, event_seq=seq, updated_at=iso(self.clock()),
                       terminal=status in TERMINAL)
        for key in flags:
            updated[key] = old[key] or event[key]
        updated.pop("error", None)
        if status in {"error", "interrupted", "rejected"}:
            updated["error"] = event.get("error") if event.get("error") in ERRORS else "internal_error"
        changes = [("job", request, old, updated)]
        lease = self.store.get("lease", GATEWAY)
        if updated["terminal"] and lease and lease["command_id"] == request:
            changes.append(("lease", GATEWAY, lease, None))
        self.store.commit(changes)


_application = None


def handler(event, context):
    global _application
    if _application is None:
        from aws_adapter import application
        _application = application()
    if "ingest_type" in event:
        return _application.ingest(event)
    return _application.http(event)