"""Arm API, transactional races, and real offline cloud-to-gateway signatures."""
import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import Mock, patch

import backend
import test_backend as fixtures


class ArmTests(fixtures.OfflineCase):
    def arm(self, **changes):
        return self.admin.arm(self.deployment(**changes))

    def no_effects(self, action):
        rows = deepcopy(self.store.rows)
        commits = deepcopy(self.store.commits)
        for service in (self.s3, self.kms, self.iot):
            service.reset_mock()
        action()
        self.assertEqual(self.store.rows, rows)
        self.assertEqual(self.store.commits, commits)
        self.assertEqual(self.s3.mock_calls + self.kms.mock_calls + self.iot.mock_calls, [])

    def test_authentication_and_authorization_on_both_routes(self):
        for method, path in (("POST", "/api/deployment-arms"),
                             ("GET", "/api/deployment-arms/" + fixtures.REQUEST)):
            for claims, status in ((None, 401), ({"cognito:groups": []}, 403),
                                   ({"scope": "openid"}, 401), ({"token_use": "id"}, 401)):
                event = self.event(method, path, self.deployment())
                if claims is None:
                    event["requestContext"].pop("authorizer")
                else:
                    event["requestContext"]["authorizer"]["jwt"]["claims"].update(claims)
                self.no_effects(lambda: self.assertEqual(self.admin.http(event)["statusCode"], status))

    def test_strict_default_off_gate_for_http_and_direct_calls(self):
        self.ready()
        self.arm()
        for value in (None, "false", "True", "TRUE", "1", "", " true", "true "):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("FIRMWARE_DEPLOYMENT_ENABLED", None)
                else:
                    os.environ["FIRMWARE_DEPLOYMENT_ENABLED"] = value
                for route, action in (("deployment-arms", self.admin.arm), ("deployments", self.admin.deploy)):
                    self.no_effects(lambda: self.fault("deployment_disabled", action,
                                                      self.deployment(), status=403))
                    response = self.admin.http(self.event("POST", "/api/" + route, {}))
                    self.assertEqual((response["statusCode"], json.loads(response["body"])),
                                     (403, {"error": "deployment_disabled"}))
                self.no_effects(lambda: self.assertEqual(self.admin.arm_status(fixtures.REQUEST)["status"], "armed"))

    def test_create_public_contract_pins_release_without_external_effects(self):
        self.ready()
        for service in (self.s3, self.kms, self.iot):
            service.reset_mock()
        response = self.admin.http(self.event("POST", "/api/deployment-arms", self.deployment()))
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"]), dict(self.deployment(), from_version="0.1.0",
            armed_at=backend.iso(self.now), expires_at=backend.iso(self.now + 120), status="armed"))
        self.assertEqual(self.s3.mock_calls + self.kms.mock_calls + self.iot.mock_calls, [])
        row = self.store.get("arm", fixtures.REQUEST)
        release = self.store.get("release", "0.2.0")
        self.assertEqual(row["release_snapshot"], release)
        self.assertEqual(row["release_sha256"], hashlib.sha256(backend.canonical(release["release"])).hexdigest())
        self.assertIsNone(self.store.get("job", fixtures.REQUEST))
        self.assertIsNone(self.store.get("lease", backend.GATEWAY))
        self.assertEqual(set(json.loads(response["body"])), set(backend.ARM_FIELDS))

    def test_replay_get_expiry_and_consumption_never_refresh(self):
        self.ready()
        original = self.arm()
        for elapsed, status in ((0, "armed"), (119.999, "armed"), (120, "expired"), (1000, "expired")):
            self.now = fixtures.NOW + elapsed
            self.no_effects(lambda: self.assertEqual(self.arm(), dict(original, status=status)))
            self.no_effects(lambda: self.assertEqual(self.admin.arm_status(fixtures.REQUEST), dict(original, status=status)))
        self.assertEqual(self.store.get("arm", fixtures.REQUEST)["status"], "armed")
        self.now = fixtures.NOW
        self.admin.deploy(self.deployment())
        self.now += 1000
        self.no_effects(lambda: self.assertEqual(self.arm(), dict(original, status="consumed")))
        response = self.admin.http(self.event("GET", "/api/deployment-arms/" + fixtures.REQUEST))
        self.assertEqual(json.loads(response["body"]), dict(original, status="consumed"))
        self.assertEqual(response["headers"]["cache-control"], "no-store")

    def test_missing_invalid_and_exact_body_validation(self):
        self.no_effects(lambda: self.fault("arm_not_found", self.admin.arm_status, fixtures.REQUEST, status=404))
        for value in ("bad", fixtures.REQUEST.replace("-", ""), True, None):
            self.no_effects(lambda: self.fault("invalid_request_id", self.admin.arm_status, value))
            self.no_effects(lambda: self.fault("invalid_request_id", self.admin.arm,
                                              self.deployment(request_id=value)))
        for extra in ({"expires_at": 9999999999}, {"from_version": "0.1.0"}, {"deployment_arm": {}}):
            response = self.admin.http(self.event("POST", "/api/deployment-arms", dict(self.deployment(), **extra)))
            self.assertEqual(json.loads(response["body"]), {"error": "invalid_body"})
        for changes in ({"device_id": backend.GATEWAY}, {"release_id": "01.2.0"}):
            self.no_effects(lambda: self.fault("invalid_deployment", self.admin.arm, self.deployment(**changes)))

    def test_arm_readiness_approval_same_version_and_busy_fail_without_writes(self):
        self.no_effects(lambda: self.fault("release_not_approved", self.admin.arm, self.deployment(), status=409))
        self.ready()
        original = self.store.get("observation", backend.GATEWAY)
        for change in ({"seen": self.now - 36}, {"seen": self.now + 1}, {"updating": True}, {"firmware": None}):
            self.store.seed("observation", backend.GATEWAY, dict(original, sub=dict(original["sub"], **change)))
            self.no_effects(lambda: self.fault("device_not_ready", self.admin.arm, self.deployment(), status=409))
        self.store.seed("observation", backend.GATEWAY, dict(original, sub=dict(original["sub"], firmware="0.2.0")))
        self.no_effects(lambda: self.fault("same_version", self.admin.arm, self.deployment(), status=409))
        self.store.seed("observation", backend.GATEWAY, original)
        self.store.seed("lease", backend.GATEWAY, {"command_id": fixtures.OTHER_REQUEST})
        self.fault("arm_conflict", self.admin.arm, self.deployment(), status=409)
        self.assertIsNone(self.store.get("arm", fixtures.REQUEST))
        self.assertEqual(self.s3.mock_calls + self.kms.mock_calls + self.iot.mock_calls, [])

    def test_request_rebinding_and_legacy_job_cannot_create_arm(self):
        self.ready()
        self.arm()
        self.no_effects(lambda: self.fault("request_id_reused", self.admin.arm,
                                          self.deployment(release_id="0.3.0"), status=409))
        self.no_effects(lambda: self.fault("arm_mismatch", self.admin.deploy,
                                          self.deployment(release_id="0.3.0"), status=409))
        self.store.seed("job", fixtures.OTHER_REQUEST, {"legacy": True})
        self.no_effects(lambda: self.fault("arm_conflict", self.admin.arm,
                                          self.deployment(request_id=fixtures.OTHER_REQUEST), status=409))

    def test_expired_future_consumed_or_missing_arm_has_no_effects(self):
        self.ready()
        self.no_effects(lambda: self.fault("arm_required", self.admin.deploy, self.deployment(), status=409))
        self.arm()
        for elapsed in (-1, 120, 900):
            self.now = fixtures.NOW + elapsed
            self.no_effects(lambda: self.fault("arm_expired", self.admin.deploy, self.deployment(), status=409))
        self.now = fixtures.NOW
        self.store.seed("arm", fixtures.REQUEST, dict(self.store.get("arm", fixtures.REQUEST), status="consumed"))
        self.no_effects(lambda: self.fault("arm_consumed", self.admin.deploy, self.deployment(), status=409))

    def test_changed_firmware_or_release_never_consumes_or_dispatches(self):
        self.ready()
        self.arm()
        original = self.store.get("observation", backend.GATEWAY)
        self.store.seed("observation", backend.GATEWAY, dict(original, sub=dict(original["sub"], firmware="0.3.0")))
        self.no_effects(lambda: self.fault("arm_mismatch", self.admin.deploy, self.deployment(), status=409))
        self.store.seed("observation", backend.GATEWAY, original)
        release = self.store.get("release", "0.2.0")
        for changes in ({"version_id": "replacement"}, {"sha256": "0" * 64},
                        {"release": dict(release["release"], signature="replacement")}, {"status": "uploaded"}):
            self.store.seed("release", "0.2.0", dict(release, **changes))
            code = "release_not_approved" if "status" in changes else "arm_mismatch"
            self.no_effects(lambda: self.fault(code, self.admin.deploy, self.deployment(), status=409))
        self.assertEqual(self.store.get("arm", fixtures.REQUEST)["status"], "armed")

    def test_fresh_observation_same_version_and_original_deadlines(self):
        self.ready()
        self.now += 0.75
        self.arm()
        self.now += 100
        observation = self.store.get("observation", backend.GATEWAY)
        self.store.seed("observation", backend.GATEWAY, dict(observation, seen=self.now,
            sub=dict(observation["sub"], seen=self.now, sequence=20)))
        self.assertEqual(self.admin.deploy(self.deployment())["status"], "dispatched")
        command = json.loads(self.iot.publish.call_args.kwargs["payload"])
        permit = json.loads(base64.b64decode(command["deployment_arm"]["payload"]))
        self.assertEqual(permit, dict(schema=1, purpose="deploy_firmware", command_id=fixtures.REQUEST,
            gateway_id=backend.GATEWAY, device_id=backend.DEVICE, version="0.2.0", from_version="0.1.0",
            sha256=command["sha256"], release_sha256=hashlib.sha256(backend.canonical(command["release"])).hexdigest(),
            issued_at=fixtures.NOW, arm_expires_at=fixtures.NOW + 120, expires_at=fixtures.NOW + 900))
        self.assertEqual(command["expires_at"], permit["expires_at"])
        self.assertGreater(command["expires_at"] - self.now, 780)
        self.assertEqual(self.store.get("arm", fixtures.REQUEST)["status"], "consumed")
        self.assertLessEqual(set(self.admin.deploy(self.deployment())), set(backend.JOB_FIELDS))
        self.iot.publish.assert_called_once()

    def test_arm_transaction_races_and_same_request_reconcile(self):
        self.ready()
        for kind, identifier in (("observation", backend.GATEWAY), ("release", "0.2.0"),
                                 ("lease", backend.GATEWAY), ("job", fixtures.REQUEST)):
            def race(changes):
                self.store.seed(kind, identifier, self.store.get(kind, identifier) or {"command_id": fixtures.OTHER_REQUEST})
            self.store.before_commit = race
            self.fault("arm_conflict", self.admin.arm, self.deployment(), status=409)
            self.assertIsNone(self.store.get("arm", fixtures.REQUEST))
            if kind in ("job", "lease"):
                self.store.rows.pop((kind, identifier))
        self.store.before_commit = lambda changes: self.store.commit(changes)
        self.assertEqual(self.arm()["status"], "armed")
        self.assertEqual(self.store.get("arm", fixtures.REQUEST)["_revision"], 1)

    def test_atomic_consumption_race_no_partial_job_or_lease(self):
        self.ready()
        self.arm()
        self.store.before_commit = lambda changes: self.store.seed("arm", fixtures.REQUEST,
            dict(self.store.get("arm", fixtures.REQUEST), status="consumed"))
        self.fault("deployment_conflict", self.admin.deploy, self.deployment(), status=409)
        self.assertIsNone(self.store.get("job", fixtures.REQUEST))
        self.assertIsNone(self.store.get("lease", backend.GATEWAY))
        self.iot.publish.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()

    def test_two_deployments_racing_for_lease_only_winner_consumes(self):
        self.ready()
        self.arm()
        self.arm(request_id=fixtures.OTHER_REQUEST)
        self.store.before_commit = lambda changes: self.admin.deploy(self.deployment(request_id=fixtures.OTHER_REQUEST))
        self.fault("deployment_conflict", self.admin.deploy, self.deployment(), status=409)
        self.assertEqual(self.store.get("arm", fixtures.REQUEST)["status"], "armed")
        self.assertEqual(self.store.get("arm", fixtures.OTHER_REQUEST)["status"], "consumed")
        self.assertIsNone(self.store.get("job", fixtures.REQUEST))
        self.iot.publish.assert_called_once()

    def test_sign_failure_burns_arm_no_retry_and_no_publish(self):
        self.ready()
        self.arm()
        self.kms.reset_mock()
        self.kms.sign.return_value = {"Signature": b"short"}
        first = self.admin.deploy(self.deployment())
        self.assertEqual(first["error"], "dispatch_preparation_failed")
        self.assertEqual(self.store.get("arm", fixtures.REQUEST)["status"], "consumed")
        self.assertIsNone(self.store.get("lease", backend.GATEWAY))
        self.assertEqual(self.admin.deploy(self.deployment()), first)
        self.kms.sign.assert_called_once()
        self.iot.publish.assert_not_called()

    def test_slow_signing_cannot_extend_arm_or_publish_expired_permit(self):
        self.ready()
        self.arm()
        def sign(**kwargs):
            self.now += 120
            return {"Signature": b"s" * 256}
        self.kms.sign.side_effect = sign
        first = self.admin.deploy(self.deployment())
        self.assertEqual(first["error"], "arm_expired")
        self.assertIsNone(self.store.get("lease", backend.GATEWAY))
        self.assertEqual(self.admin.deploy(self.deployment()), first)
        self.iot.publish.assert_not_called()

    def test_expiry_during_read_does_not_consume_or_create_job(self):
        self.ready()
        self.arm()
        get = self.store.get
        def slow_get(kind, identifier):
            result = get(kind, identifier)
            if kind == "observation":
                self.now = fixtures.NOW + 120
                result["seen"] = self.now
                result["sub"]["seen"] = self.now
            return result
        with patch.object(self.store, "get", side_effect=slow_get):
            self.no_effects(lambda: self.fault("arm_expired", self.admin.deploy, self.deployment(), status=409))

    def test_uncertain_arm_write_reconciles_read_only_without_refresh(self):
        self.ready()
        commit = self.store.commit
        def uncertain(changes):
            commit(changes)
            raise TimeoutError("private-service-detail")
        with patch.object(self.store, "commit", side_effect=uncertain):
            response = self.admin.http(self.event("POST", "/api/deployment-arms", self.deployment()))
        self.assertEqual(json.loads(response["body"]), {"error": "service_unavailable"})
        self.assertEqual(response["statusCode"], 503)
        self.now += 30
        self.no_effects(lambda: self.assertEqual(self.admin.arm_status(fixtures.REQUEST)["armed_at"], backend.iso(fixtures.NOW)))
        self.no_effects(lambda: self.assertEqual(self.arm()["expires_at"], backend.iso(fixtures.NOW + 120)))

    def test_uncertain_consumption_never_retries_dispatch(self):
        self.ready()
        self.arm()
        commit = self.store.commit
        def uncertain(changes):
            commit(changes)
            raise TimeoutError("private-service-detail")
        with patch.object(self.store, "commit", side_effect=uncertain):
            response = self.admin.http(self.event("POST", "/api/deployments", self.deployment()))
        self.assertEqual(response["statusCode"], 503)
        self.assertEqual(self.store.get("arm", fixtures.REQUEST)["status"], "consumed")
        self.assertEqual(self.store.get("lease", backend.GATEWAY)["command_id"], fixtures.REQUEST)
        self.no_effects(lambda: self.assertEqual(self.admin.deploy(self.deployment())["phase"], "dispatch_unknown"))

    def test_consumption_maps_to_one_dynamo_transaction_with_all_conditions(self):
        from aws_adapter import DynamoStore
        self.ready()
        self.arm()
        transactions = []
        commit = self.store.commit
        client = Mock(spec_set=["transact_write_items"])
        dynamo = DynamoStore(client, "offline-table")
        def capture(changes):
            transactions.append(changes)
            commit(changes)
        with patch.object(self.store, "commit", side_effect=capture):
            self.admin.deploy(self.deployment())
        dynamo.commit(transactions[0])
        client.transact_write_items.assert_called_once()
        operations = client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual([next(iter(op)) for op in operations], ["Put", "Put", "Put", "ConditionCheck", "ConditionCheck"])
        self.assertEqual([next(iter(op.values())).get("Item", next(iter(op.values())).get("Key"))["pk"]["S"]
                          for op in operations], ["arm", "job", "lease", "observation", "release"])
        self.assertEqual(operations[0]["Put"]["ConditionExpression"], "revision = :revision")
        self.assertEqual(json.loads(operations[0]["Put"]["Item"]["data"]["S"])["status"], "consumed")
        for operation in operations[1:3]:
            self.assertEqual(operation["Put"]["ConditionExpression"], "attribute_not_exists(pk)")

    def test_real_signatures_gateway_admission_restart_replay_and_expired_ack(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        gateway = str(Path(__file__).resolve().parents[1] / "gateway" / "pi-client")
        with patch.object(sys, "path", [gateway] + sys.path):
            releases = importlib.import_module("signed_release")
            arms = importlib.import_module("deployment_arm")
            jobs_module = importlib.import_module("ota_jobs")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.admin.signing_key = "arn:aws:kms:ap-southeast-2:123456789012:key/00000000-0000-0000-0000-000000000001"
        self.kms.sign.side_effect = lambda **kwargs: {"Signature": key.sign(kwargs["Message"], padding.PKCS1v15(), hashes.SHA256())}
        import boto3
        from botocore.config import Config
        s3 = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing").client(
            "s3", region_name=self.admin.region, endpoint_url="https://s3.ap-southeast-2.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
        self.s3.generate_presigned_url.side_effect = s3.generate_presigned_url
        self.ready()
        self.arm()
        self.admin.deploy(self.deployment())
        command = json.loads(self.iot.publish.call_args.kwargs["payload"])
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "public.pem"
            public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
            verifier = releases.ReleaseVerifier(public, self.admin.signing_key, self.admin.bucket, self.admin.region)
            self.assertEqual(verifier.command(command).version, "0.2.0")
            verify_arm = arms.DeploymentArmVerifier(verifier, backend.GATEWAY)
            self.assertEqual(verify_arm(command)["arm_expires_at"], fixtures.NOW + 120)
            self.assertTrue(jobs_module._ERRORS <= backend.ERRORS)
            completed = threading.Event()
            installer = Mock(side_effect=lambda *args: (completed.set() or {"status": "error", "error": "cancelled"}))
            root = Path(directory) / "journal"
            with closing(jobs_module.OtaJobs(backend.GATEWAY, root, installer, clock=lambda: self.now,
                    deployment_arm_verifier=verify_arm, require_deployment_arm=True)) as jobs:
                # Invalid authentication never creates a journal entry or starts work.
                bad_signature = dict(command, deployment_arm=dict(command["deployment_arm"],
                    signature=base64.b64encode(b"x" * 256).decode()))
                for tampered in (bad_signature, dict(command, from_version="0.3.0"),
                                 dict(command, release=dict(command["release"], key_id="other"))):
                    self.assertEqual(jobs.submit(tampered)["error"], "invalid_deployment_arm")
                self.assertEqual(list(root.glob("*.json")), [])
                installer.assert_not_called()
                accepted = jobs.submit(command)
                self.assertEqual(accepted["status"], "accepted")
                self.accepted(self.envelope("ack", accepted))
                self.assertTrue(completed.wait(5))
            self.assertEqual(installer.call_count, 1)
            self.now += 1000
            with closing(jobs_module.OtaJobs(backend.GATEWAY, root, installer, clock=lambda: self.now,
                    deployment_arm_verifier=verify_arm, require_deployment_arm=True)) as jobs:
                replay = jobs.submit(command)
                self.assertEqual(replay["error"], "cancelled")
                self.accepted(self.envelope("ack", replay))
                tampered = dict(command, from_version="0.3.0")
                self.assertEqual(jobs.submit(tampered)["error"], "command_id_reused")
            self.assertEqual(installer.call_count, 1)
            self.assertIsNone(self.store.get("lease", backend.GATEWAY))
            # Independent empty journal: authentic but late command is durable rejection.
            with closing(jobs_module.OtaJobs(backend.GATEWAY, Path(directory) / "late", installer,
                    clock=lambda: fixtures.NOW + 120, deployment_arm_verifier=verify_arm,
                    require_deployment_arm=True)) as jobs:
                rejected = jobs.submit(command)
                self.assertEqual(rejected["error"], "deployment_arm_expired")
                self.assertEqual(rejected["event_seq"], 1)
            self.assertEqual(installer.call_count, 1)
            # Test the actual expired ACK against an outstanding cloud intent.
            row = self.store.get("job", fixtures.REQUEST)
            self.store.seed("job", fixtures.REQUEST, dict(row, terminal=False, event_seq=0, phase="dispatched"))
            self.store.seed("lease", backend.GATEWAY, {"command_id": fixtures.REQUEST})
            self.accepted(self.envelope("ack", rejected))
            self.assertEqual(self.store.get("job", fixtures.REQUEST)["error"], "deployment_arm_expired")
            self.assertIsNone(self.store.get("lease", backend.GATEWAY))