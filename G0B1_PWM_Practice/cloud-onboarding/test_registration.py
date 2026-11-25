"""Offline tests. Certificates/keys exist in RAM only; no AWS or device I/O."""
import copy
import unittest
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from registration import (Conflict, LIVE_CERT, RecoveryRequired, Registration, Rejected,
                          fingerprint, import_inventory, inventory_records, set_approval,
                          validate_certificate)


ACCOUNT = "111122223333"
REGION = "ap-southeast-2"
NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)
POLICY = "isolated-bootstrap"


def certificate(key, issuer_key, issuer_name, *, thing="onboard-test-01", serial="SN-01",
                model="G0B1", mac="020000000001", ca=False, expired=False, future=False,
                duplicate_cn=False, duplicate_uri=False, client=True):
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, thing),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, serial),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, model),
    ] + ([x509.NameAttribute(NameOID.COMMON_NAME, thing)] if duplicate_cn else []))
    start = NOW + timedelta(days=1) if future else NOW - timedelta(days=2)
    end = NOW - timedelta(days=1) if expired else NOW + timedelta(days=30)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name or subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(start).not_valid_after(end)
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                           key_encipherment=False, data_encipherment=False,
                                           key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                           encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client else
                                                    ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
               .add_extension(x509.SubjectAlternativeName([
                   x509.UniformResourceIdentifier("urn:factory:mac:" + mac)
               ] * (2 if duplicate_uri else 1)), critical=False))
    return builder.sign(issuer_key, hashes.SHA256())


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


class MemoryStore:
    def __init__(self):
        self.records, self.audit = {}, []
        self.calls = 0
        self.fail_at = None
        self.fail_after = False
        self.before_commit = None

    def get(self, key):
        return copy.deepcopy(self.records.get(key))

    def commit(self, expected, changes, owner, phase, actor):
        self.calls += 1
        if self.before_commit:
            callback, self.before_commit = self.before_commit, None
            callback()
        if self.calls == self.fail_at and not self.fail_after:
            raise OSError("simulated_store_failure")
        if any(self.records.get(key) != old for key, old in expected.items()):
            raise Conflict("conditional_conflict")
        for key, data in changes.items():
            old = expected[key]
            self.records[key] = {"revision": 1 if old is None else old["revision"] + 1,
                                 "data": copy.deepcopy(data)}
        self.audit.append((owner, phase, actor))
        if self.calls == self.fail_at and self.fail_after:
            raise OSError("simulated_lost_transaction_response")


class FakeIoT:
    def __init__(self, leaf, ca):
        self.cert_id, self.ca_id = fingerprint(leaf), fingerprint(ca)
        self.arn = "arn:aws:iot:{}:{}:cert/{}".format(REGION, ACCOUNT, self.cert_id)
        self.cert = dict(certificateId=self.cert_id, certificateArn=self.arn,
                         caCertificateId=self.ca_id, status="PENDING_ACTIVATION", certificatePem=pem(leaf))
        self.ca = dict(certificateId=self.ca_id, status="ACTIVE", certificatePem=pem(ca))
        self.thing, self.things, self.policies, self.principals, self.groups = None, [], [], [], []
        self.calls, self.mutations = [], []
        self.fail_at, self.fail_after, self.after_call = None, False, None
        self.pagination = False

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if name.startswith(("describe_", "list_", "find_", "create_", "attach_", "update_")) and callable(value):
            def call(*args, **kwargs):
                self.calls.append((name, kwargs))
                number = len(self.calls)
                if number == self.fail_at and not self.fail_after:
                    raise OSError("simulated_iot_failure")
                result = value(*args, **kwargs)
                if self.after_call:
                    self.after_call(name)
                if number == self.fail_at and self.fail_after:
                    raise OSError("simulated_lost_iot_response")
                return copy.deepcopy(result)
            return call
        return value

    def describe_certificate(self, **kwargs):
        return {"certificateDescription": self.cert}

    def describe_ca_certificate(self, **kwargs):
        return {"certificateDescription": self.ca}

    def find_thing(self, name):
        return self.thing

    def create_thing(self, **kwargs):
        self.mutations.append("create")
        self.thing = {"attributes": kwargs["attributePayload"]["attributes"]}
        return {}

    def list_principal_things(self, **kwargs):
        return dict(things=self.things, **({"nextToken": "more"} if self.pagination else {}))

    def list_attached_policies(self, **kwargs):
        return {"policies": [{"policyName": name} for name in self.policies]}

    def list_thing_principals(self, **kwargs):
        return {"principals": self.principals}

    def list_thing_groups_for_thing(self, **kwargs):
        return {"thingGroups": self.groups}

    def attach_thing_principal(self, **kwargs):
        self.mutations.append("attach_thing")
        self.things, self.principals = [kwargs["thingName"]], [kwargs["principal"]]
        return {}

    def attach_policy(self, **kwargs):
        self.mutations.append("attach_policy")
        self.policies = [kwargs["policyName"]]
        return {}

    def update_certificate(self, **kwargs):
        self.mutations.append("activate")
        self.cert["status"] = kwargs["newStatus"]
        return {}


class RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca_key = ec.generate_private_key(ec.SECP256R1())
        cls.device_key = ec.generate_private_key(ec.SECP256R1())
        cls.ca = certificate(cls.ca_key, cls.ca_key, None, ca=True)
        cls.leaf = certificate(cls.device_key, cls.ca_key, cls.ca.subject)

    def setUp(self):
        self.store = MemoryStore()
        self.iot = FakeIoT(self.leaf, self.ca)
        self.records = inventory_records("lab", "SN-01", "G0B1", "02:00:00:00:00:01",
                                         "onboard-test-01", self.iot.cert_id, self.iot.ca_id)
        import_inventory(self.store, self.records, "test-admin")
        set_approval(self.store, "lab:SN-01", True, "test-admin")
        self.worker = Registration(self.store, self.iot, ACCOUNT, REGION, self.iot.ca_id, POLICY,
                                   lambda: NOW.timestamp())
        self.event = dict(awsAccountId=ACCOUNT, caCertificateId=self.iot.ca_id,
                          certificateId=self.iot.cert_id, certificateStatus="PENDING_ACTIVATION")
        self.reg_key = ("registration", self.iot.cert_id)

    def test_success_activates_last_and_audits_each_checkpoint(self):
        self.assertEqual(self.worker.handle(self.event), {"state": "BOOTSTRAP_ACTIVE", "duplicate": False})
        self.assertEqual(self.iot.mutations, ["create", "attach_thing", "attach_policy", "activate"])
        self.assertEqual([entry[1] for entry in self.store.audit],
                         ["IMPORTED", "APPROVED", "VALIDATING", "ENSURING_THING", "ATTACHING", "ACTIVATING", "BOOTSTRAP_ACTIVE"])
        self.assertEqual(self.iot.policies, [POLICY])
        self.assertEqual(self.iot.things, ["onboard-test-01"])

    def test_second_identity_uses_its_own_certificate_and_reservations(self):
        self.worker.handle(self.event)
        other_key = ec.generate_private_key(ec.SECP256R1())
        other_leaf = certificate(other_key, self.ca_key, self.ca.subject, thing="onboard-test-02",
                                 serial="SN-02", mac="020000000002")
        other_iot = FakeIoT(other_leaf, self.ca)
        records = inventory_records("lab", "SN-02", "G0B1", "020000000002", "onboard-test-02",
                                    other_iot.cert_id, other_iot.ca_id)
        import_inventory(self.store, records, "test-admin")
        set_approval(self.store, "lab:SN-02", True, "test-admin")
        worker = Registration(self.store, other_iot, ACCOUNT, REGION, other_iot.ca_id, POLICY,
                              lambda: NOW.timestamp())
        worker.handle(dict(self.event, certificateId=other_iot.cert_id))
        self.assertEqual(other_iot.things, ["onboard-test-02"])
        self.assertEqual(other_iot.principals, [other_iot.arn])
        self.assertNotEqual(other_iot.arn, self.iot.arn)
        self.assertEqual(self.store.get(("registration", other_iot.cert_id))["data"]["owner"], "lab:SN-02")

    def test_rsa_issuer_and_device_supported_weak_device_key_rejected(self):
        issuer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        issuer = certificate(issuer_key, issuer_key, None, ca=True)
        for size in (2048, 1024):
            key = rsa.generate_private_key(public_exponent=65537, key_size=size)
            leaf = certificate(key, issuer_key, issuer.subject)
            inventory = dict(self.records[("inventory", "lab:SN-01")], cert_id=fingerprint(leaf), ca_id=fingerprint(issuer))
            if size == 2048:
                validate_certificate(pem(leaf), pem(issuer), inventory, NOW.timestamp())
            else:
                with self.assertRaisesRegex(Rejected, "unsupported_key"):
                    validate_certificate(pem(leaf), pem(issuer), inventory, NOW.timestamp())

    def test_expired_or_non_ca_issuer_rejected(self):
        for change in ({"ca": True, "expired": True}, {"ca": False}):
            issuer = certificate(self.ca_key, self.ca_key, None, **change)
            leaf = certificate(self.device_key, self.ca_key, issuer.subject)
            inventory = dict(self.records[("inventory", "lab:SN-01")], cert_id=fingerprint(leaf), ca_id=fingerprint(issuer))
            with self.subTest(change=change), self.assertRaises(Rejected):
                validate_certificate(pem(leaf), pem(issuer), inventory, NOW.timestamp())

    def test_completed_replay_never_reactivates_retired_certificate(self):
        self.worker.handle(self.event)
        before = len(self.iot.calls)
        for status in ("ACTIVE", "INACTIVE", "REVOKED"):
            self.iot.cert["status"] = status
            self.assertTrue(self.worker.handle(self.event)["duplicate"])
            self.assertEqual(len(self.iot.calls), before)
            self.assertEqual(self.iot.cert["status"], status)

    def test_unapproved_and_unknown_certificates_have_no_mutations(self):
        for status in ("IMPORTED", "REJECTED"):
            self.store.records[("inventory", "lab:SN-01")]["data"]["approval"] = status
            with self.assertRaisesRegex(Rejected, "inventory_not_approved"):
                self.worker.handle(self.event)
        self.store.records.pop(("factory_cert", self.iot.cert_id))
        with self.assertRaisesRegex(Rejected, "unknown_factory_certificate"):
            self.worker.handle(self.event)
        self.assertEqual(self.iot.calls, [])
        self.assertIsNone(self.store.get(self.reg_key))

    def test_event_account_ca_status_and_live_certificate_rejected(self):
        for field, value in (("awsAccountId", "999999999999"), ("caCertificateId", "f" * 64),
                             ("certificateStatus", "ACTIVE"), ("certificateId", LIVE_CERT),
                             ("certificateId", "bad"), ("certificateId", None)):
            with self.subTest(field=field, value=value), self.assertRaises(Rejected):
                self.worker.handle(dict(self.event, **{field: value}))
        self.assertEqual(self.iot.calls, [])

    def test_authoritative_status_ca_and_arn_must_match(self):
        original = copy.deepcopy(self.iot.cert)
        for field, value in (("status", "ACTIVE"), ("status", "INACTIVE"), ("status", "REVOKED"),
                             ("caCertificateId", "f" * 64), ("certificateArn", "wrong"),
                             ("certificateId", "f" * 64)):
            self.iot.cert = dict(original, **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(Rejected):
                self.worker.handle(self.event)
        self.iot.cert = original
        self.iot.ca["status"] = "INACTIVE"
        with self.assertRaises(Rejected):
            self.worker.handle(self.event)
        self.assertEqual(self.iot.mutations, [])

    def test_signed_profile_and_validity_rejected_before_claim(self):
        for change in ({"serial": "different"}, {"model": "different"}, {"thing": "onboard-test-02"},
                       {"mac": "020000000002"}, {"duplicate_cn": True}, {"duplicate_uri": True},
                       {"expired": True}, {"future": True}, {"ca": True}, {"client": False}):
            leaf = certificate(self.device_key, self.ca_key, self.ca.subject, **change)
            inventory = dict(self.records[("inventory", "lab:SN-01")], cert_id=fingerprint(leaf))
            with self.subTest(change=change), self.assertRaises(Rejected):
                validate_certificate(pem(leaf), pem(self.ca), inventory, NOW.timestamp())
        self.iot.cert["certificatePem"] = "malformed"
        with self.assertRaises(Rejected):
            self.worker.handle(self.event)
        self.assertIsNone(self.store.get(self.reg_key))

    def test_forged_signature_and_fingerprint_rejected(self):
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        forged = certificate(self.device_key, wrong_key, self.ca.subject)
        inventory = dict(self.records[("inventory", "lab:SN-01")], cert_id=fingerprint(forged))
        with self.assertRaisesRegex(Rejected, "invalid_certificate"):
            validate_certificate(pem(forged), pem(self.ca), inventory, NOW.timestamp())
        with self.assertRaisesRegex(Rejected, "fingerprint_mismatch"):
            validate_certificate(pem(self.leaf), pem(self.ca), inventory, NOW.timestamp())

    def test_existing_thing_cannot_be_adopted(self):
        self.iot.thing = {"attributes": {"unrelated": "owner"}}
        with self.assertRaises(RecoveryRequired):
            self.worker.handle(self.event)
        self.assertEqual(self.iot.mutations, [])
        self.assertEqual(self.store.get(self.reg_key)["data"]["phase"], "RECONCILIATION_REQUIRED")

    def test_other_principals_policies_groups_and_pagination_block_activation(self):
        for field, value in (("things", ["onboard-test-02"]), ("principals", ["other-cert"]),
                             ("policies", ["operational-policy"]), ("groups", [{"groupName": "other"}]),
                             ("pagination", True)):
            self.setUp()
            setattr(self.iot, field, value)
            with self.subTest(field=field), self.assertRaises(RecoveryRequired):
                self.worker.handle(self.event)
            self.assertNotIn("activate", self.iot.mutations)
            self.assertNotIn("attach_policy", self.iot.mutations)

    def test_all_iot_failure_boundaries_replay_without_duplicate_activation(self):
        self.worker.handle(self.event)
        count = len(self.iot.calls)
        for number in range(1, count + 1):
            for after in (False, True):
                with self.subTest(call=number, after=after):
                    self.setUp()
                    self.iot.fail_at, self.iot.fail_after = number, after
                    with self.assertRaises((OSError, RecoveryRequired)):
                        self.worker.handle(self.event)
                    mutations = list(self.iot.mutations)
                    self.iot.fail_at = None
                    if self.store.get(self.reg_key) is None:
                        self.worker.handle(self.event)
                    else:
                        with self.assertRaises(RecoveryRequired):
                            self.worker.handle(self.event)
                        self.assertEqual(self.iot.mutations, mutations)
                    self.assertLessEqual(self.iot.mutations.count("activate"), 1)

    def test_all_checkpoint_failure_boundaries_preserve_permanent_claim(self):
        self.worker.handle(self.event)
        count = self.store.calls
        for number in range(3, count + 1):
            for after in (False, True):
                with self.subTest(commit=number, after=after):
                    self.setUp()
                    self.store.fail_at, self.store.fail_after = number, after
                    with self.assertRaises((OSError, RecoveryRequired)):
                        self.worker.handle(self.event)
                    mutations = list(self.iot.mutations)
                    self.store.fail_at = None
                    if self.store.get(self.reg_key) is None:
                        self.worker.handle(self.event)
                    else:
                        with self.assertRaises(RecoveryRequired):
                            self.worker.handle(self.event)
                        self.assertEqual(self.iot.mutations, mutations)

    def test_concurrent_event_loses_claim_without_iot_mutations(self):
        self.store.before_commit = lambda: self.worker.handle(self.event)
        with self.assertRaises(Conflict):
            self.worker.handle(self.event)
        self.assertEqual(self.iot.mutations.count("activate"), 1)
        self.assertTrue(self.worker.handle(self.event)["duplicate"])

    def test_approval_withdrawal_winning_claim_race_blocks_admission(self):
        self.store.before_commit = lambda: set_approval(self.store, "lab:SN-01", False, "test-admin")
        with self.assertRaises(Conflict):
            self.worker.handle(self.event)
        self.assertEqual(self.iot.mutations, [])
        self.assertIsNone(self.store.get(self.reg_key))

    def test_approval_changes_blocked_after_claim(self):
        self.worker.handle(self.event)
        for approval in (True, False):
            with self.assertRaisesRegex(Conflict, "registration_started"):
                set_approval(self.store, "lab:SN-01", approval, "test-admin")

    def test_out_of_band_approval_or_status_change_detected_before_activation(self):
        for field in ("approval", "status"):
            self.setUp()
            def change(name):
                if name == "attach_policy":
                    if field == "approval":
                        self.store.records[("inventory", "lab:SN-01")]["data"]["approval"] = "REJECTED"
                    else:
                        self.iot.cert["status"] = "REVOKED"
            self.iot.after_call = change
            with self.assertRaises(RecoveryRequired):
                self.worker.handle(self.event)
            self.assertNotIn("activate", self.iot.mutations)

    def test_reservation_or_binding_tampering_rejected(self):
        for key in (("unique_mac", "020000000001"), ("unique_thing", "onboard-test-01")):
            self.setUp()
            self.store.records[key]["data"]["owner"] = "someone-else"
            with self.assertRaises(Rejected):
                self.worker.handle(self.event)
            self.assertEqual(self.iot.calls, [])

    def test_identical_import_preserves_approval_changed_mapping_rejected(self):
        before = copy.deepcopy(self.store.records)
        import_inventory(self.store, self.records, "test-admin")
        self.assertEqual(self.store.records, before)
        changed = copy.deepcopy(self.records)
        changed[("inventory", "lab:SN-01")]["model"] = "OTHER"
        with self.assertRaises(Conflict):
            import_inventory(self.store, changed, "test-admin")
        self.assertEqual(self.store.records, before)

    def test_duplicate_mac_thing_or_fingerprint_cannot_change_owner(self):
        for mac, thing, cert_id in (("020000000001", "onboard-test-02", "a" * 64),
                                    ("020000000002", "onboard-test-01", "a" * 64),
                                    ("020000000002", "onboard-test-02", self.iot.cert_id)):
            records = inventory_records("lab", "SN-02", "G0B1", mac, thing, cert_id, self.iot.ca_id)
            with self.assertRaises(Conflict):
                import_inventory(self.store, records, "test-admin")
            self.assertIsNone(self.store.get(("inventory", "lab:SN-02")))

    def test_identity_validation(self):
        args = ["lab", "SN-01", "G0B1", "020000000001", "onboard-test-01", self.iot.cert_id, self.iot.ca_id]
        for index, value in ((0, "*"), (1, ""), (1, "x" * 65), (2, "+"), (3, "000000000000"),
                             (3, "ffffffffffff"), (3, "010000000001"), (3, "02::0000000001"),
                             (4, "pi-lab-01"), (4, "onboard-test-03"), (5, LIVE_CERT), (6, "bad")):
            values = list(args)
            values[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(Rejected):
                inventory_records(*values)


if __name__ == "__main__":
    unittest.main()