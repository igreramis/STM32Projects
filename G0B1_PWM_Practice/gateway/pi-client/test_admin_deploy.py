"""Admin -> gateway contract, offline: synthetic trust/data, no sockets or UART.

The presigner and publish transport are mocks, not AWS clients. These tests prove
the command/call path and URL-policy failure fingerprint, not the URL of a live job.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys
from unittest.mock import Mock, patch

import client
import firmware
import ota_installer
import ota_jobs
import rom_local
import rom_program
import signed_release
import uart_state
import verified_state
from deployment_arm import DeploymentArmVerifier
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from test_ota_installer import InstallerFixture
from test_signed_release import KEY_ID, envelope, key_fixture, payload, url

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[2] / "cloud-admin"), *sys.path]):
    from backend import Admin, DEVICE, GATEWAY
    from test_backend import MemoryStore


NOW = 1794780000
REQUEST = "00000000-0000-4000-8000-000000000040"


class AdminDeployTests(InstallerFixture):
    def setUp(self):
        super().setUp()
        enabled = patch.dict(os.environ, {"FIRMWARE_DEPLOYMENT_ENABLED": "true"})
        enabled.start()
        self.addCleanup(enabled.stop)
        self.guards = []
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                       "pty.openpty", "rom_program.open_port", "heartbeat_monitor.open_port"):
            guard = patch(target, side_effect=AssertionError("network/UART forbidden"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.serial.Serial.side_effect = AssertionError("UART forbidden")
        self.installer.clock = lambda: 10.0
        self.installer.wall_clock = lambda: NOW
        utc = patch.object(rom_program, "datetime")
        utc.start().now.return_value = datetime.fromtimestamp(NOW, timezone.utc)
        self.addCleanup(utc.stop)
        key, self.installer.verifier = key_fixture(self.root)
        self.image = ((0x20024000).to_bytes(4, "little") + (0x08000009).to_bytes(4, "little") +
                      b"Hello from NUCLEO-G0B1RE! firmware=0.4.0\r\n\x00")
        self.metadata = payload("0.4.0", self.image)
        self.release = envelope(key, self.metadata)
        self.installer.predecessor_state = verified_state.VerifiedState(self.root / "verified", "0.3.0")
        self.store = MemoryStore()
        self.store.seed("release", "0.4.0", dict(self.metadata, release_id="0.4.0",
                        status="approved", release=self.release))
        self.store.seed("observation", GATEWAY, dict(seen=NOW,
                        sub=dict(seen=NOW, firmware="0.3.0", updating=False)))
        self.s3 = Mock(spec_set=["generate_presigned_url"])
        self.s3.generate_presigned_url.return_value = url(self.metadata)
        self.iot = Mock(spec_set=["publish"])
        self.kms = Mock(spec_set=["sign"])
        def sign(**kwargs):
            self.assertEqual(kwargs["KeyId"], KEY_ID)
            self.assertEqual(kwargs["MessageType"], "RAW")
            self.assertEqual(kwargs["SigningAlgorithm"], "RSASSA_PKCS1_V1_5_SHA_256")
            return {"Signature": key.sign(kwargs["Message"], padding.PKCS1v15(), hashes.SHA256())}
        self.kms.sign.side_effect = sign
        self.admin = Admin(self.store, self.s3, self.kms, self.iot, self.metadata["bucket"],
                           self.metadata["region"], KEY_ID, "offline-issuer", "offline-client",
                           clock=lambda: NOW)

    def dispatch(self):
        body = dict(device_id=DEVICE, release_id="0.4.0", request_id=REQUEST)
        self.admin.arm(body)
        self.iot.publish.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()
        result = self.admin.deploy(body)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(self.iot.publish.call_count, 1)
        sent = self.iot.publish.call_args.kwargs
        self.assertEqual((sent["topic"], sent["qos"]), ("lab/pi-lab-01/commands", 1))
        self.s3.generate_presigned_url.assert_called_once_with("get_object", Params={
            "Bucket": self.metadata["bucket"], "Key": self.metadata["key"],
            "VersionId": self.metadata["version_id"]}, ExpiresIn=900)
        self.kms.sign.assert_called_once()
        self.raw = sent["payload"]
        self.command = json.loads(self.raw)
        # Boolean equality prevents a failing assertion from printing signed data.
        self.assertTrue(self.command == dict(command_id=REQUEST, command="install_firmware",
                        version="0.4.0", from_version="0.3.0", sha256=self.metadata["sha256"],
                        expires_at=NOW + 900, url=self.s3.generate_presigned_url.return_value,
                        release=self.release, deployment_arm=self.command["deployment_arm"]),
                        "Admin command contract changed")
        permit = DeploymentArmVerifier(self.installer.verifier, GATEWAY)(self.command)
        self.assertEqual((permit["issued_at"], permit["arm_expires_at"], permit["expires_at"]),
                         (NOW, NOW + 120, NOW + 900))

    def run_gateway(self):
        self.received = []
        self.validation_errors = []
        monitor = Mock(spec_set=["pause", "resume"])
        wrapped = uart_state.MonitoredInstaller(self.installer, monitor)
        jobs = ota_jobs.OtaJobs(GATEWAY, self.root / "jobs", wrapped, clock=lambda: NOW,
            deployment_arm_verifier=DeploymentArmVerifier(self.installer.verifier, GATEWAY),
            require_deployment_arm=True)
        self.addCleanup(jobs.close)
        real_command = self.installer._command
        real_validate = firmware.FirmwareStager.validate

        def command(value):
            self.received.append(deepcopy(value))
            return real_command(value)

        def validate(stager, value):
            try:
                return real_validate(stager, value)
            except firmware.StageError as exc:
                # Fixed code only: never retain/log URLs or arbitrary exception text.
                self.validation_errors.append((type(exc).__name__, str(exc)))
                raise

        with patch.object(self.installer, "_command", side_effect=command), \
                patch.object(firmware.FirmwareStager, "validate", validate), \
                patch.object(self.installer, "_download", side_effect=ota_installer.InstallerError("cancelled")) as download, \
                patch.object(rom_local, "validate_catalog", wraps=rom_local.validate_catalog) as catalog, \
                patch.object(self.installer.predecessor_state, "load",
                             wraps=self.installer.predecessor_state.load) as predecessor:
            accepted = client.CommandHandler(GATEWAY, ota=jobs).handle(self.raw, 0)
            self.assertEqual((accepted["status"], accepted["phase"], accepted["event_seq"]),
                             ("accepted", "accepted", 1))
            jobs._worker.join()
            final = dict(jobs.pending_results()[0])
            self.download_calls = download.call_count
            self.catalog_calls = catalog.call_count
            self.predecessor_calls = predecessor.call_count
        monitor.pause.assert_called_once_with()
        monitor.resume.assert_called_once_with(safe=True)
        self.assertTrue(self.received == [self.command], "Command fields changed before Installer")
        self.assertIs(final["flashed"], False)
        for flag in ("flash_verified", "flash_may_be_modified", "application_health_verified"):
            self.assertFalse(final[flag])
        self.assert_no_open()
        self.opener.open.assert_not_called()
        for guard in self.guards:
            guard.assert_not_called()
        persisted = "".join(path.read_text() for path in jobs.state_root.glob("*.json"))
        public = json.dumps(final) + persisted + self.output.getvalue()
        for secret in (self.command["url"], self.release["payload"], self.release["signature"],
                   self.command["deployment_arm"]["payload"], self.command["deployment_arm"]["signature"]):
            self.assertFalse(secret in public, "Sensitive command data leaked")
        jobs.close()
        return final

    def test_admin_signed_command_preserved_through_real_gateway_to_download_boundary(self):
        self.dispatch()
        final = self.run_gateway()
        self.assertEqual((final["error"], final["phase"], final["event_seq"]),
                         ("cancelled", "downloading", 4))
        self.assertEqual((self.catalog_calls, self.predecessor_calls, self.download_calls), (1, 1, 1))
        self.assertEqual(self.validation_errors, [])
        self.assertEqual(len(list(self.installer.run_root.glob("rom-*"))), 1)

    def test_expired_arm_gateway_ack_clears_backend_lease_without_installing(self):
        self.dispatch()
        late = NOW + 901  # Even delivery after the command deadline must terminate the job.
        self.admin.clock = lambda: late
        worker = Mock(side_effect=AssertionError("expired arm must not install"))
        jobs = ota_jobs.OtaJobs(GATEWAY, self.root / "jobs", worker, clock=lambda: late,
            deployment_arm_verifier=DeploymentArmVerifier(self.installer.verifier, GATEWAY),
            require_deployment_arm=True)
        self.addCleanup(jobs.close)
        final = client.CommandHandler(GATEWAY, ota=jobs).handle(self.raw, 0)
        self.assertEqual((final["status"], final["error"], final["event_seq"]),
                         ("rejected", "deployment_arm_expired", 1))
        self.assertIsNone(jobs._worker)
        worker.assert_not_called()
        self.assertIsNotNone(self.store.get("lease", GATEWAY))
        event = dict(final, ingest_type="ack", ingest_topic="lab/pi-lab-01/acks",
                     ingest_client_id=GATEWAY, ingest_received_at=late * 1000)
        self.assertEqual(self.admin.ingest(event), {"ok": True})
        stored = self.store.get("job", REQUEST)
        self.assertTrue(stored["terminal"])
        self.assertEqual(stored["error"], "deployment_arm_expired")
        self.assertIsNone(self.store.get("lease", GATEWAY))
        self.assertTrue(jobs.mark_delivered(REQUEST, final["event_seq"]))
        jobs.close()
        restarted = ota_jobs.OtaJobs(GATEWAY, self.root / "jobs", worker, clock=lambda: late,
            deployment_arm_verifier=DeploymentArmVerifier(self.installer.verifier, GATEWAY),
            require_deployment_arm=True)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.submit(self.command), final)
        self.assertEqual(restarted.pending_results(), [])
        worker.assert_not_called()

    def test_nonregional_url_reproduces_accepted_seq2_despite_independent_checks_passing(self):
        self.s3.generate_presigned_url.return_value = url(self.metadata).replace(
            ".s3.ap-southeast-2.amazonaws.com/", ".s3.amazonaws.com/")
        self.dispatch()
        release = self.installer.verifier.verify(self.command["release"])
        signed_release.validate_image(self.image, release)
        previous = self.installer.predecessor_state.load(self.validate())
        self.assertEqual(previous["version"], "0.3.0")
        final = self.run_gateway()
        self.assertEqual(self.validation_errors, [("StageError", "url_not_approved")])
        self.assertEqual((final["error"], final["phase"], final["event_seq"]),
                         ("image_validation_failed", "accepted", 2))
        self.assertEqual((self.catalog_calls, self.predecessor_calls, self.download_calls), (0, 0, 0))
        self.assertFalse(self.installer.run_root.exists())