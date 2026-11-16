"""Offline real RSA verification and durable single-use admission; no cloud/UART."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import rsa

import deployment_arm
import ota_installer
import ota_jobs
import signed_release
from test_signed_release import envelope, key_fixture, payload, url

NOW = 1700000000
REQUEST = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


def arm_payload(command, issued=NOW):
    return dict(schema=1, purpose="deploy_firmware", command_id=command["command_id"],
                gateway_id="pi-lab-01", device_id="stm32g0b1-01", version=command["version"],
                from_version=command["from_version"], sha256=command["sha256"],
                release_sha256=hashlib.sha256(signed_release.canonical(command["release"])).hexdigest(),
                issued_at=issued, arm_expires_at=issued + 120, expires_at=issued + 900)


class ArmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trust = tempfile.TemporaryDirectory()
        cls.key, cls.signer = key_fixture(Path(cls.trust.name))
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @classmethod
    def tearDownClass(cls):
        cls.trust.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = NOW
        self.verify = deployment_arm.DeploymentArmVerifier(self.signer, "pi-lab-01")
        self.worker = Mock(return_value={"status": "error", "error": "cancelled"})
        metadata = payload()
        self.command = dict(command_id=REQUEST, command="install_firmware", version=metadata["version"],
                            from_version="0.3.0", sha256=metadata["sha256"], expires_at=NOW + 900,
                            url=url(metadata), release=envelope(self.key, metadata))
        self.permit = arm_payload(self.command)
        self.command["deployment_arm"] = envelope(self.key, self.permit)
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                       "rom_program.open_port", "heartbeat_monitor.open_port"):
            guard = patch(target, side_effect=AssertionError("network/UART forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def jobs(self, required=True, verifier=True):
        jobs = ota_jobs.OtaJobs("pi-lab-01", self.root / "jobs", self.worker, clock=lambda: self.now,
            deployment_arm_verifier=self.verify if verifier else None, require_deployment_arm=required)
        self.addCleanup(jobs.close)
        return jobs

    def invalid(self, command):
        with self.assertRaisesRegex(ValueError, "^invalid_deployment_arm$"):
            self.verify(command)

    def test_exact_payload_and_schema_acceptance(self):
        self.assertEqual(self.verify(self.command), self.permit)
        self.assertEqual(ota_jobs.OtaJobs._command(self.command)[0], self.command)
        self.assertEqual(ota_installer.Installer._command(self.command), self.command)
        for field in ("url", "release"):
            command = {k: v for k, v in self.command.items() if k != field}
            with self.assertRaises(ValueError):
                ota_jobs.OtaJobs._command(command)
            with self.assertRaises(ota_installer.InstallerError):
                ota_installer.Installer._command(command)

    def test_all_signed_fields_have_exact_types_values_and_no_extras(self):
        invalid = {
            "schema": [True, 1.0, "1", 2], "purpose": [None, "install_firmware", "stage_firmware"],
            "command_id": [OTHER, REQUEST.replace("-", ""), "legacy"],
            "gateway_id": ["other", None], "device_id": ["stm32g0b1-02", "pi-lab-01", None],
            "version": ["7.8.8", "07.8.9", True], "from_version": ["0.2.0", "original", None],
            "sha256": ["b" * 64, "A" * 64, None], "release_sha256": ["b" * 64, "A" * 64, None],
            "issued_at": [True, float(NOW), NOW + 1, -1],
            "arm_expires_at": [True, float(NOW + 120), NOW + 119, NOW + 121],
            "expires_at": [True, float(NOW + 900), NOW + 899, NOW + 901]}
        values = [dict(self.permit, extra=True)]
        values += [{k: v for k, v in self.permit.items() if k != field} for field in self.permit]
        values += [dict(self.permit, **{field: value}) for field, options in invalid.items() for value in options]
        for value in values:
            with self.subTest(fields=set(value)):
                self.invalid(dict(self.command, deployment_arm=envelope(self.key, value)))

    def test_unsigned_command_changes_cannot_reuse_permission(self):
        for field, value in (("command_id", OTHER), ("version", "7.8.8"), ("from_version", "0.2.0"),
                             ("sha256", "b" * 64), ("expires_at", NOW + 901),
                             ("expires_at", float(NOW + 900)),
                             ("release", dict(self.command["release"], key_id="other"))):
            with self.subTest(field=field):
                self.invalid(dict(self.command, **{field: value}))
        # Canonical UUID includes lowercase, hyphenated representation.
        command = dict(self.command, command_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
        command["deployment_arm"] = envelope(self.key, arm_payload(command))
        self.invalid(command)
        with self.assertRaises(ValueError):
            deployment_arm.DeploymentArmVerifier(self.signer, "other")(self.command)

    def test_bad_signatures_and_pinned_key_before_json(self):
        bad = envelope(self.key, b"not JSON")
        bad["signature"] = base64.b64encode(b"x" * 256).decode()
        with patch.object(signed_release.json, "loads") as parse:
            self.invalid(dict(self.command, deployment_arm=bad))
            parse.assert_not_called()
        good = self.command["deployment_arm"]
        for arm in (None, {}, dict(good, extra=1), dict(good, key_id=good["key_id"] + "x"),
                    dict(good, payload="%%%"), dict(good, signature="a" * 344),
                    envelope(self.other_key, self.permit),
                    dict(good, payload=base64.b64encode(signed_release.canonical(
                        dict(self.permit, purpose="other"))).decode())):
            self.invalid(dict(self.command, deployment_arm=arm))

    def test_original_bytes_must_be_canonical_and_json_strict(self):
        raw = signed_release.canonical(self.permit)
        for value in (json.dumps(self.permit).encode(), raw + b"\n", b"\xff", b"NaN", b"[]",
                      b'{"schema":1,' + raw[1:], b"[" * 1100):
            self.invalid(dict(self.command, deployment_arm=envelope(self.key, value)))

    def test_required_missing_and_optional_invalid_never_start_worker_or_write(self):
        for required in (True, False):
            jobs = self.jobs(required=required)
            invalid = dict(self.command, deployment_arm=dict(self.command["deployment_arm"], signature="a" * 344))
            for command in ([invalid, {k: v for k, v in self.command.items() if k != "deployment_arm"}]
                            if required else [invalid]):
                result = jobs.submit(command)
                self.assertEqual(result["error"], "invalid_deployment_arm")
                self.assertIsNone(result["event_seq"])
                self.assertIsNone(jobs._worker)
                self.assertEqual(list(jobs.state_root.glob("*.json")), [])
            jobs.close()
        jobs = self.jobs(required=False, verifier=False)
        self.assertEqual(jobs.submit(self.command)["error"], "invalid_deployment_arm")
        self.worker.assert_not_called()

    def test_unsigned_extra_fields_are_not_proof(self):
        jobs = self.jobs()
        for field in ("armed", "authorized", "arm_expires_at", "gateway_id"):
            command = {k: v for k, v in self.command.items() if k != "deployment_arm"}
            command[field] = True
            self.assertEqual(jobs.submit(command)["error"], "invalid_arguments")
        self.worker.assert_not_called()

    def test_expired_authentic_terminal_survives_restart_even_after_command_expiry(self):
        for age in (120, 901):
            command = dict(self.command, command_id=REQUEST if age == 120 else OTHER)
            command["deployment_arm"] = envelope(self.key, arm_payload(command))
            self.now = NOW + age
            jobs = self.jobs()
            result = jobs.submit(command)
            self.assertEqual((result["status"], result["phase"], result["error"], result["event_seq"]),
                             ("rejected", "rejected", "deployment_arm_expired", 1))
            self.assertIsNone(jobs._worker)
            self.assertEqual(jobs.pending_results(), [result])
            self.assertTrue(jobs.mark_delivered(command["command_id"], 1))
            jobs.close()
            restarted = self.jobs()
            self.assertEqual(restarted.submit(command), result)
            restarted.close()
        self.worker.assert_not_called()

    def test_acceptance_persisted_before_thread_start_and_single_use_after_restart(self):
        jobs = self.jobs()
        def start():
            record = json.loads(next(jobs.state_root.glob("*.json")).read_text())
            self.assertEqual(record["status"], "accepted")
            self.assertEqual(record["command_id"], REQUEST)
        with patch.object(ota_jobs.threading.Thread, "start", side_effect=start) as started:
            self.assertEqual(jobs.submit(self.command)["status"], "accepted")
            started.assert_called_once()
        jobs.close()  # Simulate loss after commit, before worker executed.
        self.now += 1000
        restarted = self.jobs()
        result = restarted.submit(self.command)
        self.assertEqual((result["status"], result["error"]), ("interrupted", "interrupted"))
        self.assertEqual(restarted.submit(dict(self.command, version="7.8.8"))["error"], "command_id_reused")
        self.assertEqual(restarted.submit(dict(self.command, command_id=OTHER))["error"], "invalid_deployment_arm")
        self.worker.assert_not_called()

    def test_completed_permission_replay_never_runs_twice(self):
        jobs = self.jobs()
        self.assertEqual(jobs.submit(self.command)["status"], "accepted")
        jobs._worker.join()
        result = jobs.submit(self.command)
        jobs.close()
        self.now += 1000
        restarted = self.jobs()
        self.assertEqual(restarted.submit(self.command), result)
        self.worker.assert_called_once()
        persisted = "".join(p.read_text() for p in restarted.state_root.glob("*.json"))
        for secret in (self.command["url"], self.command["deployment_arm"]["payload"],
                       self.command["deployment_arm"]["signature"]):
            self.assertNotIn(secret, persisted)

    def test_enforcement_preserves_four_existing_legacy_jobs_and_blocks_new_unsigned_work(self):
        jobs = self.jobs(required=False, verifier=False)
        commands, results = [], []
        for index in range(4):
            command = dict(command_id="legacy-{}".format(index), command="install_firmware",
                           version="0.2.0", from_version="original", sha256="a" * 64,
                           expires_at=NOW + 900)
            commands.append(command)
            self.assertEqual(jobs.submit(command)["status"], "accepted")
            jobs._worker.join(5)
            self.assertFalse(jobs._worker.is_alive())
            result = jobs.submit(command)
            results.append(result)
            self.assertTrue(jobs.mark_delivered(command["command_id"], result["event_seq"]))
        jobs.close()
        before = {p.name: p.read_bytes() for p in jobs.state_root.glob("*.json")}
        self.assertEqual(len(before), 4)
        self.worker.reset_mock()
        self.now = NOW + 1000
        enforced = self.jobs()
        enforced.install_enabled = False
        for command, result in zip(commands, results):
            self.assertEqual(enforced.submit(command), result)
            self.assertEqual(enforced.submit(dict(command, sha256="b" * 64))["error"],
                             "command_id_reused")
        self.assertEqual(enforced.pending_results(), [])
        enforced.install_enabled = True
        self.now = NOW
        self.assertEqual(enforced.submit(dict(commands[0], command_id="new-legacy"))["error"],
                         "invalid_deployment_arm")
        unsigned = {k: v for k, v in self.command.items() if k != "deployment_arm"}
        self.assertEqual(enforced.submit(unsigned)["error"], "invalid_deployment_arm")
        self.assertIsNone(enforced._worker)
        self.worker.assert_not_called()
        self.assertEqual({p.name: p.read_bytes() for p in enforced.state_root.glob("*.json")}, before)

    def test_arm_policy_still_honors_capacity_closed_and_nonfinite_clock_gates(self):
        jobs = self.jobs()
        for now in (float("nan"), float("inf"), float("-inf")):
            self.now = now
            self.assertEqual(jobs.submit(self.command)["error"], "invalid_deployment_arm")
        for age in (0, 120):
            self.now = NOW + age
            with patch.object(ota_jobs, "MAX_RECORDS", 0):
                self.assertEqual(jobs.submit(self.command)["error"], "state_capacity_exceeded")
        self.assertEqual(list(jobs.state_root.glob("*.json")), [])
        jobs.close()
        self.assertEqual(jobs.submit(self.command)["error"], "coordinator_closed")
        self.worker.assert_not_called()

    def test_admission_boundaries_and_clock_sample_after_verification(self):
        jobs = self.jobs()
        for age in (-1.001, -2):
            self.now = NOW + age
            self.assertEqual(jobs.submit(self.command)["error"], "invalid_deployment_arm")
        self.now = NOW - 1
        self.assertEqual(jobs.submit(self.command)["status"], "accepted")
        jobs._worker.join()
        jobs.close()
        command = deepcopy(self.command)
        command["command_id"] = OTHER
        command["deployment_arm"] = envelope(self.key, arm_payload(command))
        jobs = self.jobs()
        def slow_verify(value):
            result = self.verify(value)
            self.now = NOW + 120
            return result
        jobs.deployment_arm_verifier = slow_verify
        self.now = NOW + 119
        self.assertEqual(jobs.submit(command)["error"], "deployment_arm_expired")
        self.assertIsNone(jobs._worker)

    def test_emergency_disable_and_storage_failure_cannot_consume_and_run(self):
        jobs = self.jobs()
        jobs.install_enabled = False
        self.assertEqual(jobs.submit(self.command)["error"], "deployment_disabled")
        self.assertEqual(list(jobs.state_root.glob("*.json")), [])
        jobs.install_enabled = True
        with patch.object(jobs, "_atomic_write", side_effect=OSError("disk failure")):
            self.assertEqual(jobs.submit(self.command)["error"], "storage_error")
        self.assertIsNone(jobs._worker)
        self.worker.assert_not_called()