"""Offline contract/regression tests: python3 -B -m unittest discover -s cloud-admin.

Only standard-library dependencies. AWS effects are strict mocks; the store models
atomic revision-checked transactions. Gateway producers run without constructors
that open MQTT/UART, workers, configuration files, or persistent journals.
"""
import base64
from copy import deepcopy
import importlib
import io
import json
from pathlib import Path
import struct
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import backend
from backend import Admin, Conflict, DEVICE, Fault, GATEWAY, canonical, iso


NOW = 1700000000
REQUEST = "00000000-0000-4000-8000-000000000001"
OTHER_REQUEST = "00000000-0000-4000-8000-000000000002"


def image(version="0.2.0", sp=0x20024000, reset=0x08000009):
    return struct.pack("<II", sp, reset) + b"firmware=" + version.encode() + b"\0" + b"\xff" * 64


class MemoryStore:
    """Deep-copy reads, atomic CAS, and explicit condition checks like DynamoStore."""

    def __init__(self):
        self.rows = {}
        self.commits = []
        self.before_commit = None

    def get(self, kind, identifier):
        return deepcopy(self.rows.get((kind, identifier)))

    def list(self, kind):
        return [deepcopy(row) for (key, _), row in self.rows.items() if key == kind]

    def commit(self, changes):
        self.commits.append(deepcopy(changes))
        if self.before_commit:
            hook, self.before_commit = self.before_commit, None
            hook(changes)
        for kind, identifier, before, _ in changes:
            current = self.rows.get((kind, identifier))
            if ((before is None and current is not None) or
                    (before is not None and (current is None or
                     current["_revision"] != before["_revision"]))):
                raise Conflict()
        for kind, identifier, before, after in changes:
            if before is not None and after is before:
                continue
            if after is None:
                self.rows.pop((kind, identifier), None)
            else:
                self.rows[(kind, identifier)] = dict(deepcopy(after),
                    _revision=1 if before is None else before["_revision"] + 1)

    def seed(self, kind, identifier, row):
        self.commit([(kind, identifier, self.get(kind, identifier), row)])


class OfflineCase(unittest.TestCase):
    def test_disabled_deployment_has_no_external_effects(self):
        commits_before = deepcopy(self.store.commits)
        self.iot.reset_mock()
        self.s3.reset_mock()
        self.kms.reset_mock()
        with patch.dict("os.environ", {"FIRMWARE_DEPLOYMENT_ENABLED": "false"}):
            response = self.admin.http(self.event("POST", "/api/deployments", {}))
        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(json.loads(response["body"]), {"error": "deployment_disabled"})
        self.assertEqual(self.store.commits, commits_before)
        self.iot.publish.assert_not_called()
        self.s3.put_object.assert_not_called()
        self.kms.sign.assert_not_called()

    def setUp(self):
        # Fail loudly if a future test accidentally acquires a network transport.
        for target in ("socket.socket", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.now = NOW
        self.store = MemoryStore()
        self.s3 = Mock(spec_set=["put_object", "get_object", "generate_presigned_url"])
        self.kms = Mock(spec_set=["sign"])
        self.iot = Mock(spec_set=["publish"])
        self.s3.put_object.return_value = {"VersionId": "object-version-1"}
        self.s3.generate_presigned_url.return_value = "https://offline.invalid/image?versionId=object-version-1"
        self.kms.sign.return_value = {"Signature": b"s" * 256}
        self.admin = Admin(self.store, self.s3, self.kms, self.iot, "offline-bucket",
                           "ap-southeast-2", "offline-signing-key", "offline-issuer",
                           "offline-client", clock=lambda: self.now)

    def fault(self, code, function, *args, status=400):
        with self.assertRaises(Fault) as caught:
            function(*args)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def event(self, method="GET", path="/api/devices", body=None):
        return {"requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": {
            "token_use": "access", "iss": "offline-issuer", "client_id": "offline-client",
            "scope": "openid admin-api/access", "cognito:groups": ["admins"]}}}},
            "rawPath": path, "body": json.dumps(body) if body is not None else ""}

    def upload_body(self, data=None, **changes):
        result = dict(version="0.2.0", filename="firmware.bin",
                      content_base64=base64.b64encode(image() if data is None else data).decode())
        result.update(changes)
        return result

    def approved(self):
        self.admin.upload(self.upload_body())
        self.s3.get_object.return_value = {"VersionId": "object-version-1", "Body": io.BytesIO(image())}
        self.admin.approve("0.2.0")
        return self.store.get("release", "0.2.0")

    def ready(self):
        self.approved()
        self.store.seed("observation", GATEWAY, {"seen": self.now, "boot_id": "boot-a", "count": 1,
            "sub": {"seen": self.now, "firmware": "0.1.0", "state": "idle", "sequence": 0,
                    "updating": False}})

    def deployment(self, **changes):
        result = dict(device_id=DEVICE, release_id="0.2.0", request_id=REQUEST)
        result.update(changes)
        return result

    def envelope(self, kind, payload):
        return dict(payload, ingest_type=kind, ingest_topic="lab/pi-lab-01/" +
                    ("telemetry" if kind == "telemetry" else "acks"),
                    ingest_client_id=GATEWAY, ingest_received_at=int(self.now * 1000))

    def telemetry(self, source=None, observed=None, **changes):
        source = self.now if source is None else source
        observed = source if observed is None else observed
        result = self.envelope("telemetry", dict(device_id=GATEWAY, timestamp=iso(source),
            boot_id="boot-a", telemetry_count=1, subdevices=[dict(device_id=DEVICE,
            firmware="0.1.0", state="idle", sequence=0, last_seen=iso(observed),
            age_seconds=source - observed, updating=False)]))
        result.update(changes)
        return result

    def ack(self, **changes):
        result = self.envelope("ack", dict(device_id=GATEWAY, command_id=REQUEST, version="0.2.0",
            event_seq=1, status="accepted", phase="accepted", flash_verified=False,
            application_health_verified=False))
        result.update(changes)
        return result

    def accepted(self, event):
        self.assertEqual(self.admin.ingest(event), {"ok": True})

    def rejected(self, event):
        before = deepcopy(self.store.rows)
        self.assertEqual(self.admin.ingest(event), {"ok": False, "error": "event_rejected"})
        self.assertEqual(self.store.rows, before)


class AuthBodyTests(OfflineCase):
    def test_preflight_is_empty_and_has_no_data_access(self):
        for path in ("/api/devices", "/api/releases", "/api/releases/0.2.0/approve",
                     "/api/deployments"):
            event = self.event("OPTIONS", path)
            event["requestContext"].pop("authorizer")
            with patch.object(self.admin, "store") as store:
                response = self.admin.http(event)
                self.assertEqual(response["statusCode"], 204)
                self.assertEqual(response["body"], "")
                self.assertEqual(store.mock_calls, [])
        self.assertEqual(self.s3.mock_calls + self.kms.mock_calls + self.iot.mock_calls, [])
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            event = self.event(method)
            event["requestContext"].pop("authorizer")
            self.assertEqual(self.admin.http(event)["statusCode"], 401)

    def test_authorizer_claims_required_header_is_not_trusted(self):
        event = self.event()
        event["requestContext"].pop("authorizer")
        event["headers"] = {"authorization": "Bearer not-a-token"}
        self.assertEqual(self.admin.http(event)["statusCode"], 401)
        for field, value in (("token_use", "id"), ("iss", "other"), ("client_id", "other"),
                             ("scope", "admin-api/access-extra")):
            with self.subTest(field=field):
                event = self.event()
                event["requestContext"]["authorizer"]["jwt"]["claims"][field] = value
                self.assertEqual(self.admin.http(event)["statusCode"], 401)
        self.assertEqual(self.store.commits, [])
        self.assertEqual(self.s3.mock_calls + self.kms.mock_calls + self.iot.mock_calls, [])

    def test_admin_group_exact_membership_and_authorizer_encodings(self):
        for groups in (["admins"], '["admins"]', "[admins,operators]", "admins"):
            with self.subTest(groups=groups):
                event = self.event()
                event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = groups
                self.assertEqual(self.admin.http(event)["statusCode"], 200)
        for groups in ([], ["notadmins"], "notadmins", {}, None, 1):
            with self.subTest(groups=groups):
                event = self.event()
                event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = groups
                self.assertEqual(self.admin.http(event)["statusCode"], 403)

    def test_body_exact_fields_and_strict_json(self):
        for raw in ('{"x":1,"x":2}', '{"x":{"a":1,"a":2}}', '{"x":NaN}',
                    '{"x":Infinity}', '{"x":-Infinity}', '{', '[' * 2000):
            with self.subTest(raw=raw[:40]):
                self.fault("invalid_json", self.admin.body, {"body": raw}, ("x",))
        for raw in ('[]', 'null', '1', '"x"', '{}', '{"x":1,"extra":2}', None, " " * 40001):
            with self.subTest(raw=str(raw)[:40]):
                self.fault("invalid_body", self.admin.body, {"body": raw}, ("x",))
        self.fault("invalid_body", self.admin.body, {"body": '{"x":1}', "isBase64Encoded": True}, ("x",))
        self.assertEqual(self.admin.body({"body": '{"x":1}'}, ("x",)), {"x": 1})

    def test_http_routes_projection_and_sanitized_errors(self):
        self.approved()
        self.store.seed("release", "0.3.0", {"status": "reserved"})
        response = self.admin.http(self.event(path="/api/releases"))
        self.assertEqual(response["headers"]["cache-control"], "no-store")
        rows = json.loads(response["body"])["releases"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), set(backend.RELEASE_FIELDS))
        self.assertEqual(self.admin.http(self.event(path="/missing"))["statusCode"], 404)
        with patch.object(self.store, "list", side_effect=RuntimeError("untrusted-service-detail")):
            response = self.admin.http(self.event(path="/api/releases"))
        self.assertEqual(response["statusCode"], 503)
        self.assertEqual(json.loads(response["body"]), {"error": "service_unavailable"})
        with patch.object(self.store, "list", side_effect=Conflict):
            self.assertEqual(self.admin.http(self.event(path="/api/releases"))["statusCode"], 409)


class UploadApprovalTests(OfflineCase):
    def test_upload_immutable_versioned_object_and_public_projection(self):
        response = self.admin.http(self.event("POST", "/api/releases", self.upload_body()))
        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(set(json.loads(response["body"])), set(backend.RELEASE_FIELDS))
        self.s3.put_object.assert_called_once_with(Bucket="offline-bucket",
            Key="firmware/stm32g0b1/0.2.0/firmware.bin", Body=image(),
            ContentType="application/octet-stream", IfNoneMatch="*")
        row = self.store.get("release", "0.2.0")
        self.assertEqual((row["status"], row["version_id"]), ("uploaded", "object-version-1"))
        self.fault("duplicate_version", self.admin.upload, self.upload_body(), status=409)
        self.assertEqual(self.s3.put_object.call_count, 1)

    def test_upload_invalid_metadata_and_base64_have_no_effects(self):
        for changes in ({"version": "01.2.0"}, {"version": True}, {"filename": "../image.bin"},
                        {"filename": "image.hex"}, {"filename": "a" * 105 + ".bin"},
                        {"content_base64": "!!!!"}, {"content_base64": "Y Q=="},
                        {"content_base64": None}, {"content_base64": "x" * 27309}):
            with self.subTest(changes=changes.keys()):
                self.fault("invalid_upload", self.admin.upload, self.upload_body(**changes))
        self.assertEqual(self.store.commits, [])
        self.s3.put_object.assert_not_called()

    def test_binary_size_vectors_and_exact_firmware_tokens(self):
        for data, code in ((b"x" * 7, "invalid_firmware"), (b"x" * 20481, "invalid_firmware"),
                           (image(sp=0x20024008), "invalid_vectors"),
                           (image(sp=0x20000001), "invalid_vectors"),
                           (image(reset=0x08000008), "invalid_vectors"),
                           (image(reset=0x08010001), "invalid_vectors"),
                           (image("0.2.01"), "firmware_version_mismatch"),
                           (image("0.2.0-evil"), "firmware_version_mismatch"),
                           (image() + b"firmware=0.3.0\0", "firmware_version_mismatch"),
                           (image().replace(b"firmware=", b"not-fw=="), "firmware_version_mismatch")):
            with self.subTest(code=code, data=data[:24]):
                self.fault(code, self.admin.upload, self.upload_body(data))
        self.assertEqual(self.store.commits, [])
        self.s3.put_object.assert_not_called()
        self.assertEqual(len(backend.binary(image() + b"\xff" * (20480 - len(image())), "0.2.0")), 64)

    def test_uncertain_upload_burns_version_without_retry(self):
        self.s3.put_object.side_effect = TimeoutError("untrusted-detail")
        event = self.event("POST", "/api/releases", self.upload_body())
        self.assertEqual(self.admin.http(event)["statusCode"], 503)
        self.assertEqual(self.store.get("release", "0.2.0")["status"], "reserved")
        self.assertEqual(self.admin.http(event)["statusCode"], 409)
        self.s3.put_object.assert_called_once()

    def test_missing_object_version_burns_version(self):
        for value in (None, "", "null"):
            with self.subTest(value=value):
                self.store.rows.clear()
                self.s3.put_object.return_value = {"VersionId": value}
                self.fault("bucket_versioning_required", self.admin.upload, self.upload_body(), status=503)
                self.fault("duplicate_version", self.admin.upload, self.upload_body(), status=409)
                self.assertEqual(self.store.get("release", "0.2.0")["status"], "reserved")
        self.assertEqual(self.s3.put_object.call_count, 3)

    def test_post_upload_commit_conflict_does_not_repeat_s3(self):
        def put(**kwargs):
            self.store.before_commit = lambda changes: (_ for _ in ()).throw(Conflict())
            return {"VersionId": "object-version-1"}
        self.s3.put_object.side_effect = put
        event = self.event("POST", "/api/releases", self.upload_body())
        self.assertEqual(self.admin.http(event)["statusCode"], 409)
        self.assertEqual(self.admin.http(event)["statusCode"], 409)
        self.s3.put_object.assert_called_once()

    def test_approval_revalidates_pinned_version_and_signs_canonical_payload(self):
        row = self.approved()
        self.s3.get_object.assert_called_once_with(Bucket="offline-bucket", Key=row["key"],
                                                  VersionId="object-version-1")
        self.assertTrue(self.s3.get_object.return_value["Body"].closed)
        expected = dict(schema=1, target=backend.TARGET, version="0.2.0", size_bytes=len(image()),
                        sha256=backend.binary(image(), "0.2.0"), bucket="offline-bucket",
                        region="ap-southeast-2", key=row["key"], version_id="object-version-1")
        payload = canonical(expected)
        self.kms.sign.assert_called_once_with(KeyId="offline-signing-key", Message=payload,
            MessageType="RAW", SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256")
        self.assertEqual(base64.b64decode(row["release"]["payload"]), payload)
        self.assertEqual(base64.b64decode(row["release"]["signature"]), b"s" * 256)
        before = len(self.store.commits)
        result = self.admin.approve("0.2.0")
        self.assertEqual(set(result), set(backend.RELEASE_FIELDS))
        self.assertEqual(len(self.store.commits), before)
        self.kms.sign.assert_called_once()
        self.s3.get_object.assert_called_once()

    def test_approval_integrity_failures_never_sign(self):
        self.admin.upload(self.upload_body())
        for data, object_version in ((image(), "other-version"), (image() + b"x", "object-version-1"),
                                     (image()[:-1] + b"x", "object-version-1"),
                                     (image("0.3.0"), "object-version-1")):
            with self.subTest(object_version=object_version, data=data[:24]):
                stream = io.BytesIO(data)
                self.s3.get_object.return_value = {"VersionId": object_version, "Body": stream}
                with self.assertRaises(Fault):
                    self.admin.approve("0.2.0")
                self.assertTrue(stream.closed)
                self.assertEqual(self.store.get("release", "0.2.0")["status"], "uploaded")
        self.kms.sign.assert_not_called()

    def test_approval_closes_stream_on_read_failure(self):
        self.admin.upload(self.upload_body())
        stream = Mock(spec_set=["read", "close"])
        stream.read.side_effect = OSError("untrusted-detail")
        self.s3.get_object.return_value = {"VersionId": "object-version-1", "Body": stream}
        response = self.admin.http(self.event("POST", "/api/releases/0.2.0/approve"))
        self.assertEqual(response["statusCode"], 503)
        stream.read.assert_called_once_with(20481)
        stream.close.assert_called_once()
        self.kms.sign.assert_not_called()

    def test_approval_unknown_reserved_and_bad_signature(self):
        self.fault("release_not_found", self.admin.approve, "0.2.0", status=404)
        self.store.seed("release", "0.3.0", {"status": "reserved"})
        self.fault("release_not_found", self.admin.approve, "0.3.0", status=404)
        self.admin.upload(self.upload_body())
        self.s3.get_object.return_value = {"VersionId": "object-version-1", "Body": io.BytesIO(image())}
        self.kms.sign.return_value = {"Signature": b"short"}
        self.fault("signing_failed", self.admin.approve, "0.2.0", status=503)
        self.assertEqual(self.store.get("release", "0.2.0")["status"], "uploaded")


class DeploymentTests(OfflineCase):
    def test_success_durable_intent_before_one_publish_and_idempotent_replay(self):
        self.ready()
        def publish(**kwargs):
            job = self.store.get("job", REQUEST)
            self.assertEqual((job["phase"], job["terminal"]), ("dispatch_unknown", False))
            self.assertEqual(self.store.get("lease", GATEWAY)["command_id"], REQUEST)
        self.iot.publish.side_effect = publish
        first = self.admin.deploy(self.deployment())
        self.assertEqual(first["status"], "dispatched")
        self.assertNotIn("error", first)
        self.assertLessEqual(set(first), set(backend.JOB_FIELDS))
        self.now += 1000  # Even when no longer ready, replay returns the existing job.
        self.assertEqual(self.admin.deploy(self.deployment()), first)
        self.iot.publish.assert_called_once()
        self.s3.generate_presigned_url.assert_called_once_with("get_object", Params={
            "Bucket": "offline-bucket", "Key": "firmware/stm32g0b1/0.2.0/firmware.bin",
            "VersionId": "object-version-1"}, ExpiresIn=900)
        args = self.iot.publish.call_args.kwargs
        self.assertEqual((args["topic"], args["qos"]), ("lab/pi-lab-01/commands", 1))
        command = json.loads(args["payload"])
        self.assertEqual(set(command), {"command_id", "command", "version", "from_version", "sha256",
                                        "expires_at", "url", "release"})
        self.assertEqual(command["expires_at"], NOW + 900)
        self.assertEqual(command["from_version"], "0.1.0")
        self.assertEqual(args["payload"], canonical(command))
        self.assertNotIn("url", self.store.get("job", REQUEST))

    def test_invalid_deployments_have_no_effects(self):
        for request in (None, True, "bad", REQUEST.replace("-", "")):
            with self.subTest(request=request):
                with self.assertRaises(Fault):
                    self.admin.deploy(self.deployment(request_id=request))
        for changes in ({"device_id": GATEWAY}, {"release_id": "01.2.0"}):
            self.fault("invalid_deployment", self.admin.deploy, self.deployment(**changes))
        self.fault("release_not_approved", self.admin.deploy, self.deployment(), status=409)
        self.admin.upload(self.upload_body())
        self.fault("release_not_approved", self.admin.deploy, self.deployment(), status=409)
        self.iot.publish.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()

    def test_readiness_freshness_future_time_updating_and_firmware(self):
        self.ready()
        good = self.store.get("observation", GATEWAY)
        self.store.rows.pop(("observation", GATEWAY))
        self.fault("device_not_ready", self.admin.deploy, self.deployment(), status=409)
        cases = [dict(good, seen=NOW - 36), dict(good, seen=NOW + 1)]
        for change in ({"seen": NOW - 36}, {"seen": NOW + 1}, {"seen": None},
                       {"firmware": None}, {"updating": True}):
            cases.append(dict(good, sub=dict(good["sub"], **change)))
        for row in cases:
            with self.subTest(row=row):
                self.store.seed("observation", GATEWAY, row)
                self.fault("device_not_ready", self.admin.deploy, self.deployment(), status=409)
        self.store.seed("observation", GATEWAY, dict(good, sub=dict(good["sub"], firmware="0.2.0")))
        self.fault("same_version", self.admin.deploy, self.deployment(), status=409)
        self.iot.publish.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()
        self.store.seed("observation", GATEWAY, dict(good, seen=NOW - 35,
                        sub=dict(good["sub"], seen=NOW - 35)))
        self.assertEqual(self.admin.deploy(self.deployment())["status"], "dispatched")

    def test_request_reuse_and_active_lease_prevent_publish(self):
        self.ready()
        self.admin.deploy(self.deployment())
        self.fault("request_id_reused", self.admin.deploy, self.deployment(release_id="0.3.0"), status=409)
        self.fault("deployment_conflict", self.admin.deploy, self.deployment(request_id=OTHER_REQUEST), status=409)
        self.assertIsNone(self.store.get("job", OTHER_REQUEST))
        self.iot.publish.assert_called_once()
        self.s3.generate_presigned_url.assert_called_once()

    def test_transaction_checks_read_observation_and_release_before_effects(self):
        for kind, identifier in (("observation", GATEWAY), ("release", "0.2.0")):
            with self.subTest(kind=kind):
                self.store = MemoryStore()
                self.admin.store = self.store
                self.ready()
                def race(changes):
                    row = self.store.get(kind, identifier)
                    self.store.seed(kind, identifier, row)
                self.store.before_commit = race
                self.fault("deployment_conflict", self.admin.deploy, self.deployment(), status=409)
                self.assertIsNone(self.store.get("job", REQUEST))
                self.assertIsNone(self.store.get("lease", GATEWAY))
        self.iot.publish.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()

    def test_concurrent_same_request_returns_winner_without_publish(self):
        self.ready()
        def winner(changes):
            self.store.commit(changes)
        self.store.before_commit = winner
        result = self.admin.deploy(self.deployment())
        self.assertEqual(result["phase"], "dispatch_unknown")
        self.iot.publish.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()

    def test_ambiguous_publish_is_never_retried_and_lease_does_not_expire(self):
        self.ready()
        self.iot.publish.side_effect = TimeoutError("untrusted-detail")
        first = self.admin.deploy(self.deployment())
        self.assertEqual((first["status"], first["error"]), ("error", "dispatch_unknown"))
        self.now += 86400
        self.assertEqual(self.admin.deploy(self.deployment()), first)
        self.assertFalse(self.store.get("job", REQUEST)["terminal"])
        self.assertEqual(self.store.get("lease", GATEWAY)["command_id"], REQUEST)
        self.iot.publish.assert_called_once()
        self.accepted(self.ack(status="error", phase="error", error="command_expired"))
        self.assertIsNone(self.store.get("lease", GATEWAY))
        self.assertEqual(self.admin.deploy(self.deployment())["error"], "command_expired")
        self.iot.publish.assert_called_once()

    def test_preparation_failure_releases_lease_without_publish_or_retry(self):
        self.ready()
        self.s3.generate_presigned_url.side_effect = RuntimeError("untrusted-detail")
        result = self.admin.deploy(self.deployment())
        self.assertEqual(result["error"], "dispatch_preparation_failed")
        self.assertTrue(self.store.get("job", REQUEST)["terminal"])
        self.assertIsNone(self.store.get("lease", GATEWAY))
        self.assertEqual(self.admin.deploy(self.deployment()), result)
        self.s3.generate_presigned_url.assert_called_once()
        self.iot.publish.assert_not_called()

    def test_oversize_command_releases_lease_without_publish(self):
        self.ready()
        self.s3.generate_presigned_url.return_value = "x" * 8192
        result = self.admin.deploy(self.deployment())
        self.assertEqual(result["error"], "command_too_large")
        self.assertTrue(self.store.get("job", REQUEST)["terminal"])
        self.assertIsNone(self.store.get("lease", GATEWAY))
        self.iot.publish.assert_not_called()

    def test_ack_during_publish_wins_over_dispatch_result(self):
        self.ready()
        self.iot.publish.side_effect = lambda **kwargs: self.accepted(self.ack())
        self.assertEqual(self.admin.deploy(self.deployment())["phase"], "accepted")
        self.assertEqual(self.store.get("job", REQUEST)["event_seq"], 1)
        self.iot.publish.assert_called_once()

    def test_ack_racing_dispatched_commit_wins_without_republish(self):
        self.ready()
        def published(**kwargs):
            self.store.before_commit = lambda changes: self.accepted(self.ack())
        self.iot.publish.side_effect = published
        self.assertEqual(self.admin.deploy(self.deployment())["phase"], "accepted")
        self.iot.publish.assert_called_once()


class IngestTests(OfflineCase):
    def assert_clock_rejection(self, event, code, delta):
        malicious = "untrusted-secret\nWARNING: forged-log https://evil.invalid/?token=secret"
        event["diagnostic_padding"] = malicious
        with self.assertLogs("backend", level="WARNING") as captured:
            self.rejected(event)
        self.assertEqual(captured.output, [
            "WARNING:backend:ingest_rejected code=" + code + " delta_seconds=" + str(delta)])
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelname, "WARNING")
        self.assertEqual(record.args, (delta,))
        self.assertTrue(all(type(value) in (int, float) for value in record.args))
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)
        for forbidden in (malicious, "untrusted-secret", "forged-log", "token=secret",
                          GATEWAY, DEVICE, REQUEST, event.get("boot_id", "boot-a")):
            self.assertNotIn(forbidden, captured.output[0])

    def test_broker_clock_rejection_logs_only_signed_numeric_delta(self):
        for kind in ("telemetry", "ack"):
            for delta in (-0.125, 35.125):
                with self.subTest(kind=kind, delta=delta):
                    event = self.telemetry() if kind == "telemetry" else self.ack()
                    event["ingest_received_at"] = int((NOW - delta) * 1000)
                    with patch.object(self.admin, "clock", return_value=NOW) as clock:
                        self.assert_clock_rejection(event, "broker_clock", delta)
                    clock.assert_called_once_with()

    def test_source_clock_rejection_logs_only_signed_numeric_delta(self):
        for delta in (-0.100001, -0.101, -0.125, -1, 35.000001, 35.125):
            with self.subTest(delta=delta):
                event = self.telemetry(source=NOW - delta)
                actual_delta = NOW - backend.timestamp(event["timestamp"])
                self.assertAlmostEqual(actual_delta, delta, places=6)
                self.assert_clock_rejection(event, "source_clock", actual_delta)

    def test_observation_future_rejection_logs_only_signed_numeric_delta(self):
        event = self.telemetry(observed=NOW + 0.125)
        event["subdevices"][0]["age_seconds"] = 0
        self.assert_clock_rejection(event, "observation_future", -0.125)

    def test_observation_age_rejection_logs_only_signed_numeric_delta(self):
        for age, delta in ((6, 4.0), (14, -4.0)):
            with self.subTest(age=age):
                event = self.telemetry(observed=NOW - 10)
                event["subdevices"][0]["age_seconds"] = age
                self.assert_clock_rejection(event, "observation_age", delta)

    def test_clock_boundaries_still_accepted_without_warning(self):
        with patch.object(backend.LOGGER, "warning") as warning:
            for broker_delta in (0, 35):
                for source_delta in (-0.1, 0, 35):
                    for observation_delta, age in ((0, 0), (0, 3), (3, 0), (2**31, 2**31)):
                        with self.subTest(broker=broker_delta, source=source_delta,
                                          observed=observation_delta, age=age):
                            self.store.rows.clear()
                            received = NOW - broker_delta
                            source = received - source_delta
                            event = self.telemetry(source=source, observed=source - observation_delta,
                                                   ingest_received_at=received * 1000)
                            event["subdevices"][0]["age_seconds"] = age
                            self.accepted(event)
            warning.assert_not_called()

    def test_source_clock_tolerance_is_inclusive(self):
        # At the epoch subtraction is exactly -0.1, without large-float cancellation.
        self.now = 0
        event = self.telemetry(source=0.1)
        self.assertEqual(event["ingest_received_at"] / 1000 -
                         backend.timestamp(event["timestamp"]), -0.1)
        with patch.object(backend.LOGGER, "warning") as warning:
            self.accepted(event)
            warning.assert_not_called()
        observation = self.store.get("observation", GATEWAY)
        self.assertEqual(observation["seen"], 0.1)
        self.assertEqual(observation["sub"]["seen"], 0.1)
        self.assertEqual(self.store.get("boot", "boot-a")["first_seen"], 0.1)

    def test_source_clock_fractional_millisecond_samples_preserve_timestamps(self):
        # Measured deltas and a fractional-millisecond example from the Pi offset.
        for ahead in (0.006, 0.022, 0.024796):
            with self.subTest(ahead=ahead):
                self.store.rows.clear()
                event = self.telemetry(source=NOW + ahead, observed=NOW + ahead - 0.003123)
                with patch.object(backend.LOGGER, "warning") as warning:
                    self.accepted(event)
                    warning.assert_not_called()
                observation = self.store.get("observation", GATEWAY)
                self.assertEqual(iso(observation["seen"]), event["timestamp"])
                self.assertEqual(iso(observation["sub"]["seen"]),
                                 event["subdevices"][0]["last_seen"])

    def test_tolerated_future_sample_stays_offline_until_clock_catches_up(self):
        source, observed = NOW + 0.022, NOW + 0.006
        self.accepted(self.telemetry(source=source, observed=observed))
        self.assertFalse(any(row["online"] for row in self.admin.devices()))
        self.now = observed
        self.assertFalse(any(row["online"] for row in self.admin.devices()))
        self.now = source
        self.assertTrue(all(row["online"] for row in self.admin.devices()))
        self.assertEqual([row["last_seen"] for row in self.admin.devices()],
                         [iso(source), iso(observed)])

    def test_tolerated_source_skew_does_not_relax_observation_future(self):
        source = NOW + 0.022
        event = self.telemetry(source=source, observed=source + 0.000001)
        event["subdevices"][0]["age_seconds"] = 0
        delta = backend.timestamp(event["timestamp"]) - backend.timestamp(
            event["subdevices"][0]["last_seen"])
        self.assert_clock_rejection(event, "observation_future", delta)

    def test_tolerated_source_skew_never_refreshes_uart_by_mqtt(self):
        observed = NOW + 0.006
        self.accepted(self.telemetry(source=NOW + 0.022, observed=observed))
        for count, elapsed in enumerate((10, 35, 36), start=2):
            with self.subTest(elapsed=elapsed):
                self.now = NOW + elapsed
                source = self.now + 0.022
                self.accepted(self.telemetry(source=source, observed=observed, telemetry_count=count))
                self.now = source  # Visibility still waits for the original source timestamp.
                observation = self.store.get("observation", GATEWAY)
                self.assertEqual(observation["seen"], source)
                self.assertEqual(observation["sub"]["seen"], observed)
                devices = self.admin.devices()
                self.assertTrue(devices[0]["online"])
                self.assertEqual(devices[1]["online"], elapsed == 10)
                self.assertEqual(devices[1]["last_seen"], iso(observed))

    def test_alternating_source_clock_transit_stays_fresh_with_increasing_counts(self):
        # Span more than the freshness window, alternating ahead/behind broker time.
        for count, delta in enumerate((-0.022, 0.006, -0.006, 0.022, -0.024796, 0.001), start=1):
            with self.subTest(count=count, delta=delta):
                self.now = NOW + (count - 1) * 10
                source = self.now - delta
                observed = source - 0.001
                event = self.telemetry(source=source, observed=observed, telemetry_count=count)
                self.accepted(event)
                observation = self.store.get("observation", GATEWAY)
                self.assertEqual((observation["seen"], observation["count"], observation["boot_id"]),
                                 (source, count, "boot-a"))
                self.assertEqual(iso(observation["sub"]["seen"]),
                                 event["subdevices"][0]["last_seen"])
                self.assertEqual(observation["_revision"], count)
                self.assertEqual([row["online"] for row in self.admin.devices()],
                                 [delta >= 0, delta >= 0])
                self.now = max(self.now, source)
                self.assertTrue(all(row["online"] for row in self.admin.devices()))

    def test_tolerated_source_skew_preserves_replay_and_boot_semantics(self):
        self.accepted(self.telemetry(source=NOW + 0.006, telemetry_count=2))
        original = deepcopy(self.store.rows)
        for event in (self.telemetry(source=NOW + 0.006, telemetry_count=3),
                      self.telemetry(source=NOW + 0.001, telemetry_count=3),
                      self.telemetry(source=NOW + 0.022, telemetry_count=2),
                      self.telemetry(source=NOW + 0.022, telemetry_count=1)):
            self.accepted(event)
            self.assertEqual(self.store.rows, original)
        self.accepted(self.telemetry(source=NOW + 0.022, boot_id="boot-b", telemetry_count=0))
        observation = self.store.get("observation", GATEWAY)
        self.assertEqual((observation["boot_id"], observation["count"]), ("boot-b", 0))
        original = deepcopy(self.store.rows)
        self.accepted(self.telemetry(source=NOW + 0.03, boot_id="boot-a", telemetry_count=100))
        self.assertEqual(self.store.rows, original)

    def test_schema_rejections_stay_sanitized_without_warning(self):
        malicious = "untrusted-secret\nWARNING: forged-log token=secret"
        with patch.object(backend.LOGGER, "warning") as warning:
            for changes in ({"ingest_received_at": malicious}, {"ingest_received_at": True},
                            {"ingest_received_at": -1}, {"ingest_received_at": 2**53 + 1},
                            {"ingest_type": malicious}, {"device_id": malicious},
                            {"timestamp": malicious}, {"boot_id": malicious},
                            {"telemetry_count": malicious}):
                self.rejected(self.telemetry(**changes))
            for changes in ({"last_seen": malicious}, {"age_seconds": malicious},
                            {"age_seconds": True}, {"age_seconds": -1},
                            {"age_seconds": 2**31 + 1}, {"age_seconds": float("nan")},
                            {"age_seconds": float("inf")}, {"firmware": malicious},
                            {"state": malicious}, {"state": []}, {"sequence": malicious},
                            {"device_id": malicious}, {"last_seen": None}):
                event = self.telemetry()
                event["subdevices"][0].update(changes)
                self.rejected(event)
            # Invalid boot/count/age still short-circuit before their clock gates.
            self.rejected(self.telemetry(source=NOW + 1, boot_id=malicious))
            self.rejected(self.telemetry(source=NOW + 1, telemetry_count=malicious))
            self.rejected(self.telemetry(observed=NOW + 1))
            warning.assert_not_called()

    def test_untrusted_exception_text_is_never_logged(self):
        malicious = "untrusted-secret\nWARNING: forged-log token=secret"
        with patch.object(backend.LOGGER, "warning") as warning:
            for error in (Fault, Conflict, ValueError, TypeError, KeyError, OverflowError):
                with self.subTest(error=error.__name__):
                    with patch.object(self.store, "get", side_effect=error(malicious)):
                        self.rejected(self.telemetry())
            with patch.object(self.store, "get", side_effect=RuntimeError(malicious)):
                with self.assertRaisesRegex(RuntimeError, "^ingest_unavailable$"):
                    self.admin.ingest(self.telemetry())
            warning.assert_not_called()

    def test_trusted_identity_received_time_and_payload_bounds(self):
        for changes in ({"ingest_type": "other"}, {"ingest_topic": "lab/other/telemetry"},
                        {"ingest_client_id": "other"}, {"device_id": "other"},
                        {"ingest_received_at": True}, {"ingest_received_at": NOW * 1000 + 1},
                        {"ingest_received_at": (NOW - 36) * 1000},
                        {"ingest_received_at": 2**53 + 1}, {"padding": "x" * 8192},
                        {"extra": float("nan")}):
            with self.subTest(changes=changes.keys()):
                self.rejected(self.telemetry(**changes))
        self.accepted(self.telemetry(source=NOW - 35, ingest_received_at=(NOW - 35) * 1000))

    def test_invalid_telemetry_schema_and_observation(self):
        for changes in ({"boot_id": "../bad"}, {"boot_id": "x" * 65}, {"telemetry_count": True},
                        {"telemetry_count": -1}, {"telemetry_count": 2**53 + 1},
                        {"timestamp": iso(NOW + 1)}, {"timestamp": iso(NOW - 36)},
                        {"timestamp": "2023-11-14T22:13:20"}, {"subdevices": []}):
            with self.subTest(changes=changes):
                self.rejected(self.telemetry(**changes))
        for changes in ({"device_id": "other"}, {"firmware": "bad"}, {"state": "unknown"},
                        {"state": []}, {"sequence": True}, {"sequence": -1}, {"sequence": 2**32},
                        {"sequence": None}, {"state": None}, {"updating": 1}, {"extra": 1},
                        {"age_seconds": True}, {"age_seconds": -1}, {"age_seconds": 4},
                        {"last_seen": iso(NOW + 1)}, {"last_seen": None}):
            with self.subTest(changes=changes):
                event = self.telemetry()
                event["subdevices"][0].update(changes)
                self.rejected(event)

    def test_monotonic_source_count_and_retired_boot_ids(self):
        self.accepted(self.telemetry())
        original = deepcopy(self.store.rows)
        self.now += 1
        for event in (self.telemetry(source=NOW, telemetry_count=2),
                      self.telemetry(telemetry_count=1), self.telemetry(telemetry_count=0)):
            self.accepted(event)
            self.assertEqual(self.store.rows, original)
        self.accepted(self.telemetry(boot_id="boot-b", telemetry_count=0))
        self.assertEqual(self.store.get("observation", GATEWAY)["boot_id"], "boot-b")
        self.now += 1
        before = deepcopy(self.store.rows)
        self.accepted(self.telemetry(boot_id="boot-a", telemetry_count=100))
        self.assertEqual(self.store.rows, before)

    def test_new_boot_without_uart_does_not_inherit_previous_observation(self):
        self.accepted(self.telemetry())
        self.now += 1
        event = self.telemetry(boot_id="boot-b", telemetry_count=0)
        event["subdevices"][0].update(firmware=None, state=None, sequence=None, last_seen=None, age_seconds=None)
        self.accepted(event)
        devices = self.admin.devices()
        self.assertTrue(devices[0]["online"])
        self.assertFalse(devices[1]["online"])
        self.assertIsNone(devices[1]["last_seen"])
        self.assertIsNone(devices[1]["firmware"])

    def test_old_uart_samples_do_not_regress_but_sequence_wrap_is_allowed(self):
        self.accepted(self.telemetry())
        self.now += 1
        event = self.telemetry(observed=NOW - 1, telemetry_count=2)
        event["subdevices"][0].update(firmware="0.3.0", state="fault", sequence=99, updating=True)
        self.accepted(event)
        sub = self.store.get("observation", GATEWAY)["sub"]
        self.assertEqual((sub["seen"], sub["firmware"], sub["state"]), (NOW, "0.1.0", "idle"))
        self.assertTrue(sub["updating"])
        for seq in (2**32 - 1, 0):
            self.now += 1
            event = self.telemetry(telemetry_count=int(self.now - NOW + 2))
            event["subdevices"][0]["sequence"] = seq
            self.accepted(event)
            self.assertEqual(self.store.get("observation", GATEWAY)["sub"]["sequence"], seq)

    def test_devices_freshness_boundaries_and_parent_dependency(self):
        self.assertFalse(any(row["online"] for row in self.admin.devices()))
        self.accepted(self.telemetry())
        self.now += 35
        self.assertTrue(all(row["online"] for row in self.admin.devices()))
        self.now += 0.001
        self.assertFalse(any(row["online"] for row in self.admin.devices()))
        self.now = NOW - 1
        self.assertFalse(any(row["online"] for row in self.admin.devices()))

    def test_ingest_conflict_rejected_and_service_failure_sanitized_for_retry(self):
        with patch.object(self.store, "commit", side_effect=Conflict):
            self.rejected(self.telemetry())
        with patch.object(self.store, "get", side_effect=RuntimeError("untrusted-detail")):
            with self.assertRaisesRegex(RuntimeError, "^ingest_unavailable$"):
                self.admin.ingest(self.telemetry())


class AckTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.ready()
        self.admin.deploy(self.deployment())

    def test_duplicate_out_of_order_unknown_and_wrong_version_are_noops(self):
        self.accepted(self.ack(event_seq=2, status="programming", phase="programming"))
        before = deepcopy(self.store.rows)
        for changes in ({"event_seq": 2}, {"event_seq": 1}, {"command_id": OTHER_REQUEST},
                        {"version": "0.3.0", "event_seq": 3}):
            self.accepted(self.ack(**changes))
            self.assertEqual(self.store.rows, before)
        self.rejected(self.ack(event_seq=3))  # Higher sequence cannot regress phase.

    def test_invalid_ack_flags_sequence_status_phase_and_success_claim(self):
        for changes in ({"event_seq": None}, {"event_seq": True}, {"event_seq": 0},
                        {"event_seq": 2**53 + 1}, {"command_id": "x" * 65},
                        {"status": "unknown"}, {"phase": "unknown"}, {"flash_verified": 1},
                        {"application_health_verified": None}, {"application_health_verified": True},
                        {"status": "application_verified", "phase": "application_verified"}):
            with self.subTest(changes=changes):
                self.rejected(self.ack(**changes))

    def test_flags_sticky_terminal_immutable_and_matching_lease_released(self):
        self.accepted(self.ack(event_seq=2, status="flash_verified", phase="flash_verified", flash_verified=True))
        self.accepted(self.ack(event_seq=3, status="waiting_for_application", phase="waiting_for_application"))
        self.assertTrue(self.store.get("job", REQUEST)["flash_verified"])
        self.assertIsNotNone(self.store.get("lease", GATEWAY))
        self.accepted(self.ack(event_seq=4, status="application_verified", phase="application_verified",
                               flash_verified=True, application_health_verified=True))
        job = self.store.get("job", REQUEST)
        self.assertTrue(job["terminal"])
        self.assertTrue(job["application_health_verified"])
        self.assertIsNone(self.store.get("lease", GATEWAY))
        self.accepted(self.ack(event_seq=5, status="error", phase="error", error="internal_error"))
        self.assertEqual(self.store.get("job", REQUEST), job)
        self.assertEqual(self.admin.deploy(self.deployment())["status"], "application_verified")
        self.iot.publish.assert_called_once()

    def test_terminal_ack_cannot_delete_another_jobs_lease(self):
        self.store.seed("lease", GATEWAY, {"command_id": OTHER_REQUEST})
        self.accepted(self.ack(status="error", phase="error", error="untrusted-error-text"))
        self.assertEqual(self.store.get("job", REQUEST)["error"], "internal_error")
        self.assertEqual(self.store.get("lease", GATEWAY)["command_id"], OTHER_REQUEST)

    def test_error_and_interrupted_preserve_last_phase_and_evidence(self):
        self.accepted(self.ack(status="flash_verified", phase="flash_verified", flash_verified=True))
        self.accepted(self.ack(event_seq=2, status="interrupted", phase="flash_verified",
                               flash_verified=True, error="interrupted"))
        job = self.store.get("job", REQUEST)
        self.assertEqual((job["phase"], job["error"]), ("flash_verified", "interrupted"))
        self.assertTrue(job["flash_verified"])
        self.assertIsNone(self.store.get("lease", GATEWAY))

    def test_terminal_lease_race_is_atomic_and_redelivery_can_succeed(self):
        def race(changes):
            self.store.seed("lease", GATEWAY, {"command_id": OTHER_REQUEST})
        self.store.before_commit = race
        event = self.ack(status="rejected", phase="rejected", error="installer_busy")
        self.assertEqual(self.admin.ingest(event), {"ok": False, "error": "event_rejected"})
        self.assertFalse(self.store.get("job", REQUEST)["terminal"])
        self.accepted(event)
        self.assertTrue(self.store.get("job", REQUEST)["terminal"])
        self.assertEqual(self.store.get("lease", GATEWAY)["command_id"], OTHER_REQUEST)


class GatewayCompatibilityTests(OfflineCase):
    def setUp(self):
        super().setUp()
        gateway_path = str(Path(__file__).resolve().parents[1] / "gateway" / "pi-client")
        # Only SDK imports are stubbed, not telemetry, parsers, or ACK producers.
        crt, iot = ModuleType("awscrt"), ModuleType("awsiot")
        crt.mqtt = Mock()
        iot.mqtt_connection_builder = Mock()
        for guard in (patch.dict(sys.modules, {"awscrt": crt, "awsiot": iot}),
                      patch.object(sys, "path", [gateway_path] + sys.path)):
            guard.start()
            self.addCleanup(guard.stop)
        self.client = importlib.import_module("client")
        self.uart = importlib.import_module("uart_state")
        self.jobs = importlib.import_module("ota_jobs")
        self.rom = importlib.import_module("rom_program")
        self.monitor = self.uart.StateMonitor("/dev/ttyAMA0", clock=lambda: self.now - NOW,
            wall_clock=lambda: self.now, opener=Mock(side_effect=AssertionError("UART forbidden")))
        self.gateway = self.client.Gateway.__new__(self.client.Gateway)
        self.gateway.config = SimpleNamespace(client_id=GATEWAY)
        self.gateway.boot_id = "actual-gateway-boot"
        self.gateway.monitor = self.monitor
        self.gateway.ota = None

    def produced(self, count):
        # Keep the real snapshot shape and producer, replacing only wall time and /proc I/O.
        with patch.object(self.client, "datetime") as date, patch.object(self.client.Path, "read_text", return_value="123.0 456.0"):
            date.now.return_value.isoformat.return_value = iso(self.now)
            payload = self.gateway.telemetry(count)
        return self.envelope("telemetry", payload)

    def state(self, **changes):
        sample = dict(type="state", device_id=DEVICE, firmware="0.1.0", state="running", sequence=42)
        sample.update(changes)
        self.monitor.feed(canonical(sample) + b"\r\n")

    def test_actual_empty_gateway_and_active_install_telemetry(self):
        self.gateway.ota = SimpleNamespace(status=lambda: {"active": True})
        self.accepted(self.produced(1))
        devices = self.admin.devices()
        self.assertTrue(devices[0]["online"])
        self.assertFalse(devices[1]["online"])
        self.assertTrue(devices[1]["updating"])
        self.assertIsNone(devices[1]["firmware"])

    def test_actual_uart_state_and_legacy_heartbeat_both_ingest(self):
        self.state()
        self.accepted(self.produced(1))
        self.assertEqual(self.admin.devices()[1]["state"], "running")
        self.now += 1
        self.monitor.feed(b"Hello from NUCLEO-G0B1RE! firmware=0.3.0\r\n")
        event = self.produced(2)
        self.assertIsNone(event["subdevices"][0]["state"])
        self.assertIsNone(event["subdevices"][0]["sequence"])
        self.accepted(event)
        sub = self.admin.devices()[1]
        self.assertEqual(sub["firmware"], "0.3.0")
        self.assertIsNone(sub["state"])
        self.assertTrue(sub["online"])

    def test_actual_legacy_heartbeat_allows_deploy_without_operating_state(self):
        self.approved()
        self.monitor.feed(b"Hello from NUCLEO-G0B1RE! firmware=0.1.0\r\n")
        self.accepted(self.produced(1))
        self.assertEqual(self.admin.deploy(self.deployment())["status"], "dispatched")
        self.iot.publish.assert_called_once()

    def test_actual_republication_expires_state_without_advancing_uart_seen(self):
        self.state()
        self.accepted(self.produced(1))
        seen = self.admin.devices()[1]["last_seen"]
        self.now += 35
        self.accepted(self.produced(2))
        sub = self.admin.devices()[1]
        self.assertEqual(sub["last_seen"], seen)
        self.assertIsNone(sub["state"])
        self.assertIsNone(self.store.get("observation", GATEWAY)["sub"]["sequence"])
        self.now += 1
        self.accepted(self.produced(3))
        self.assertTrue(self.admin.devices()[0]["online"])
        self.assertFalse(self.admin.devices()[1]["online"])

    def test_actual_heartbeats_do_not_keep_operating_state_alive(self):
        self.state()
        self.accepted(self.produced(1))
        for count, elapsed in ((2, 10), (3, 35)):
            self.now = NOW + elapsed
            self.monitor.feed(b"Hello from NUCLEO-G0B1RE! firmware=0.1.0\n")
            self.accepted(self.produced(count))
            sub = self.admin.devices()[1]
            self.assertTrue(sub["online"])
            self.assertEqual(sub["state"], "running" if elapsed == 10 else None)

    def test_backend_vectors_reject_empty_stack_at_sram_base_like_gateway(self):
        data = image(sp=0x20000000)
        with self.assertRaises(self.rom.ProbeError):
            self.rom.validate_vectors(data)
        self.fault("invalid_vectors", self.admin.upload, self.upload_body(data))
        self.s3.put_object.assert_not_called()

    def coordinator(self):
        # Exercise real progress/finish/snapshot transformations with only persistence mocked.
        jobs = self.jobs.OtaJobs.__new__(self.jobs.OtaJobs)
        jobs.client_id = GATEWAY
        jobs._lock = threading.RLock()
        jobs.stop_event = threading.Event()
        jobs._healthy = True
        jobs._records = {REQUEST: dict(command_id=REQUEST, device_id=GATEWAY, version="0.2.0",
            fingerprint="0" * 64, status="accepted", phase="accepted", flashed=False,
            flash_verified=False, flash_may_be_modified=False, application_health_verified=False,
            delivery_seq=1, acked_seq=0)}
        jobs._atomic_write = Mock()
        return jobs

    def test_actual_gateway_command_parser_and_ack_progress_to_success(self):
        self.ready()
        self.admin.deploy(self.deployment())
        payload = self.iot.publish.call_args.kwargs["payload"]
        command = self.client.parse_command_object(payload)
        validated, _ = self.jobs.OtaJobs._command(command)
        self.assertEqual(validated, command)
        jobs = self.coordinator()
        self.accepted(self.envelope("ack", jobs._snapshot(jobs._records[REQUEST])))
        for phase in backend.PHASES[1:]:
            jobs._progress(REQUEST, phase, flash_verified=phase in
                           {"flash_verified", "waiting_for_application", "application_verified"})
            self.accepted(self.envelope("ack", jobs._snapshot(jobs._records[REQUEST])))
            self.assertIsNotNone(self.store.get("lease", GATEWAY))
        jobs._finish(REQUEST, {"status": "application_verified", "application_health_verified": True})
        self.accepted(self.envelope("ack", jobs._snapshot(jobs._records[REQUEST])))
        self.assertTrue(self.store.get("job", REQUEST)["application_health_verified"])
        self.assertIsNone(self.store.get("lease", GATEWAY))

    def test_actual_gateway_error_ack_and_transient_rejection(self):
        self.ready()
        self.admin.deploy(self.deployment())
        jobs = self.coordinator()
        self.rejected(self.envelope("ack", jobs._reject(REQUEST, "command_expired")))
        self.assertIsNotNone(self.store.get("lease", GATEWAY))
        jobs._progress(REQUEST, "flash_verified", flash_verified=True)
        self.accepted(self.envelope("ack", jobs._snapshot(jobs._records[REQUEST])))
        jobs._finish(REQUEST, {"status": "error", "error": "application_start_timeout"})
        self.accepted(self.envelope("ack", jobs._snapshot(jobs._records[REQUEST])))
        job = self.store.get("job", REQUEST)
        self.assertEqual((job["phase"], job["error"]), ("flash_verified", "application_start_timeout"))
        self.assertTrue(job["flash_verified"])
        self.assertFalse(job["application_health_verified"])
        self.assertIsNone(self.store.get("lease", GATEWAY))


if __name__ == "__main__":
    unittest.main()