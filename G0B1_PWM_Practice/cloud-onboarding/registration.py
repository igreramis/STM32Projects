"""Isolated JITR admission. No AWS clients, credentials or device I/O on import.

A permanent operation claim deliberately favours a recovery hold over a stale
worker reactivating credentials. There is no timed lease takeover.
"""
import re
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


TEST_THINGS = ("onboard-test-01", "onboard-test-02")
LIVE_CERT = "f3190d109b88f9234702070d246c4d109f83012b48748fe193165371933e10d0"
HASH = re.compile(r"[0-9a-f]{64}\Z")
LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class Rejected(Exception):
    """Only fixed, non-sensitive reason codes may be passed to this exception."""


class Conflict(Exception):
    """A conditional write lost; reload state, never overwrite it."""


class RecoveryRequired(Exception):
    """A recorded operation needs inspection before it may proceed."""


def fingerprint(cert):
    return cert.fingerprint(hashes.SHA256()).hex()


def inventory_records(manufacturer, serial, model, mac, thing, cert_id, ca_id):
    """Prepare an IMPORTED bundle, not an approval or an AWS write.

    The approved importer must transact all records with absence conditions.
    Device keys may be generated on-device or by a controlled factory station.
    """
    if not all(isinstance(v, str) and LABEL.fullmatch(v)
               for v in (manufacturer, serial, model)):
        raise Rejected("invalid_identity")
    if thing not in TEST_THINGS:
        raise Rejected("test_identity_only")
    if not all(isinstance(v, str) and HASH.fullmatch(v) for v in (cert_id, ca_id)):
        raise Rejected("invalid_fingerprint")
    if cert_id == LIVE_CERT:
        raise Rejected("protected_certificate")
    if not isinstance(mac, str):
        raise Rejected("invalid_mac")
    # Accept either twelve hex characters or six consistently separated octets.
    if not re.fullmatch(r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})", mac):
        raise Rejected("invalid_mac")
    mac = mac.replace(":", "").replace("-", "").lower()
    if int(mac[:2], 16) & 1 or mac == "000000000000":
        raise Rejected("invalid_mac")
    owner = manufacturer + ":" + serial
    inventory = dict(manufacturer=manufacturer, serial=serial, model=model,
                     mac=mac, thing=thing, cert_id=cert_id, ca_id=ca_id,
                     owner=owner, approval="IMPORTED")
    return {
        ("inventory", owner): inventory,
        ("unique_mac", mac): {"owner": owner},
        ("unique_thing", thing): {"owner": owner},
        ("factory_cert", cert_id): {"owner": owner, "ca_id": ca_id},
    }


def import_inventory(store, records, actor):
    """Internal admin primitive. No public/admin endpoint is exposed in phase one."""
    current = {key: store.get(key) for key in records}
    if any(value is not None for value in current.values()):
        # An identical reimport is harmless; it must not revert an approval.
        for key, data in records.items():
            existing = current[key]
            if existing is None:
                raise Conflict("inventory_collision")
            comparable = dict(existing["data"])
            if key[0] == "inventory":
                comparable["approval"] = "IMPORTED"
            if comparable != data:
                raise Conflict("inventory_collision")
        return
    owner = next(data["owner"] for key, data in records.items() if key[0] == "inventory")
    store.commit(current, records, owner, "IMPORTED", actor)


def set_approval(store, owner, approved, actor):
    """Serialize decisions with admission: approval freezes once work is claimed.

    After that point withdrawal requires a separately reviewed lifecycle flow,
    not an expiring lease or a direct table edit.
    """
    key = ("inventory", owner)
    record = store.get(key)
    if record is None or type(approved) is not bool:
        raise Rejected("invalid_approval")
    reg_key = ("registration", record["data"]["cert_id"])
    if store.get(reg_key) is not None:
        raise Conflict("registration_started")
    data = dict(record["data"], approval="APPROVED" if approved else "REJECTED")
    store.commit({key: record, reg_key: None}, {key: data}, owner, data["approval"], actor)


def _one(subject, oid):
    entries = subject.get_attributes_for_oid(oid)
    if len(entries) != 1:
        raise Rejected("ambiguous_subject")
    return entries[0].value


def validate_certificate(pem, issuer_pem, inventory, now):
    """Strict lab leaf profile and direct issuing-CA signature validation."""
    try:
        leaf = x509.load_pem_x509_certificate(pem.encode("ascii"))
        issuer = x509.load_pem_x509_certificate(issuer_pem.encode("ascii"))
        if fingerprint(leaf) != inventory["cert_id"] or fingerprint(issuer) != inventory["ca_id"]:
            raise Rejected("fingerprint_mismatch")
        instant = datetime.fromtimestamp(now, timezone.utc)
        for cert in (leaf, issuer):
            if not cert.not_valid_before_utc <= instant < cert.not_valid_after_utc:
                raise Rejected("certificate_expired_or_future")
        if leaf.issuer != issuer.subject:
            raise Rejected("issuer_mismatch")
        if not issuer.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise Rejected("issuer_not_ca")
        if not issuer.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
            raise Rejected("issuer_cannot_sign")
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise Rejected("leaf_is_ca")
        if not leaf.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature:
            raise Rejected("leaf_cannot_sign")
        if ExtendedKeyUsageOID.CLIENT_AUTH not in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise Rejected("not_client_certificate")
        if _one(leaf.subject, NameOID.COMMON_NAME) != inventory["thing"] or \
                _one(leaf.subject, NameOID.SERIAL_NUMBER) != inventory["serial"] or \
                _one(leaf.subject, NameOID.ORGANIZATIONAL_UNIT_NAME) != inventory["model"]:
            raise Rejected("signed_identity_mismatch")
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if list(names) != [x509.UniformResourceIdentifier("urn:factory:mac:" + inventory["mac"])]:
            raise Rejected("signed_mac_mismatch")
        for key in (leaf.public_key(), issuer.public_key()):
            if isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048:
                continue
            if isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name in ("secp256r1", "secp384r1", "secp521r1"):
                continue
            raise Rejected("unsupported_key")
        algorithm = leaf.signature_hash_algorithm
        if not isinstance(algorithm, (hashes.SHA256, hashes.SHA384, hashes.SHA512)):
            raise Rejected("unsupported_signature")
        key = issuer.public_key()
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(leaf.signature, leaf.tbs_certificate_bytes, padding.PKCS1v15(), algorithm)
        else:
            key.verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(algorithm))
    except Rejected:
        raise
    except Exception:
        # Never return certificate contents or parser errors to logs.
        raise Rejected("invalid_certificate") from None


class Registration:
    def __init__(self, store, iot, account, region, ca_id, policy, clock, partition="aws"):
        if not re.fullmatch(r"[0-9]{12}", account) or not HASH.fullmatch(ca_id):
            raise ValueError("invalid_configuration")
        self.store, self.iot = store, iot
        self.account, self.region, self.ca_id = account, region, ca_id
        self.policy, self.clock, self.partition = policy, clock, partition

    def handle(self, event):
        if not isinstance(event, dict) or event.get("awsAccountId") != self.account or \
                event.get("caCertificateId") != self.ca_id or \
                event.get("certificateStatus") != "PENDING_ACTIVATION":
            raise Rejected("invalid_registration_event")
        cert_id = event.get("certificateId")
        if not isinstance(cert_id, str) or not HASH.fullmatch(cert_id) or cert_id == LIVE_CERT:
            raise Rejected("invalid_certificate_id")
        reg_key = ("registration", cert_id)
        previous = self.store.get(reg_key)
        if previous is not None:
            # Never re-run mutations, including activation, for duplicate events.
            if previous["data"]["phase"] == "BOOTSTRAP_ACTIVE":
                return {"state": "BOOTSTRAP_ACTIVE", "duplicate": True}
            raise RecoveryRequired("registration_claimed")

        binding_key = ("factory_cert", cert_id)
        binding = self.store.get(binding_key)
        if binding is None or binding["data"].get("ca_id") != self.ca_id:
            raise Rejected("unknown_factory_certificate")
        inv_key = ("inventory", binding["data"]["owner"])
        inv_record = self.store.get(inv_key)
        if inv_record is None or inv_record["data"].get("approval") != "APPROVED":
            raise Rejected("inventory_not_approved")
        inventory = inv_record["data"]
        # Revalidate stored identifiers as well as their binding before IoT calls.
        canonical = inventory_records(*(inventory[name] for name in
                                      ("manufacturer", "serial", "model", "mac", "thing", "cert_id", "ca_id")))
        if inventory["cert_id"] != cert_id or inventory["ca_id"] != self.ca_id or \
                inventory["owner"] != binding["data"]["owner"] or \
                inv_key not in canonical:
            raise Rejected("inventory_binding_mismatch")
        expected = {reg_key: None, binding_key: binding, inv_key: inv_record}
        for key in (("unique_mac", inventory["mac"]), ("unique_thing", inventory["thing"])):
            reservation = self.store.get(key)
            if reservation is None or reservation["data"] != {"owner": inventory["owner"]}:
                raise Rejected("identity_reservation_mismatch")
            expected[key] = reservation

        cert = self.iot.describe_certificate(certificateId=cert_id)["certificateDescription"]
        arn = "arn:{}:iot:{}:{}:cert/{}".format(self.partition, self.region, self.account, cert_id)
        if cert.get("certificateId") != cert_id or cert.get("certificateArn") != arn or \
                cert.get("caCertificateId") != self.ca_id or cert.get("status") != "PENDING_ACTIVATION":
            raise Rejected("certificate_not_pending")
        ca = self.iot.describe_ca_certificate(certificateId=self.ca_id)["certificateDescription"]
        if ca.get("status") != "ACTIVE" or ca.get("certificateId") != self.ca_id:
            raise Rejected("factory_ca_not_active")
        validate_certificate(cert["certificatePem"], ca["certificatePem"], inventory, self.clock())
        state = {"phase": "VALIDATING", "owner": inventory["owner"], "thing": inventory["thing"],
                 "cert_id": cert_id, "certificate_arn": arn}
        self.store.commit(expected, {reg_key: state}, inventory["owner"], "VALIDATING", "registration")
        try:
            self._admit(reg_key, inventory, arn)
        except Exception:
            # An SDK response can be lost AFTER a successful remote mutation.
            # Keep the permanent claim even when this checkpoint also fails.
            try:
                self._phase(reg_key, "RECONCILIATION_REQUIRED")
            except Exception:
                pass
            raise RecoveryRequired("external_outcome_requires_inspection") from None
        return {"state": "BOOTSTRAP_ACTIVE", "duplicate": False}

    def _phase(self, key, phase):
        old = self.store.get(key)
        data = dict(old["data"], phase=phase)
        self.store.commit({key: old}, {key: data}, data["owner"], phase, "registration")

    def _attachments(self, arn, thing):
        things = self.iot.list_principal_things(principal=arn)
        policies = self.iot.list_attached_policies(target=arn)
        principals = self.iot.list_thing_principals(thingName=thing)
        if things.get("nextToken") or policies.get("nextMarker") or principals.get("nextToken"):
            raise Rejected("attachment_collision")
        names = things.get("things", [])
        policy_names = [p["policyName"] for p in policies.get("policies", [])]
        ids = principals.get("principals", [])
        if any(name != thing for name in names) or any(p != self.policy for p in policy_names) or \
                any(p != arn for p in ids):
            raise Rejected("attachment_collision")
        # No group memberships: these can grant additional inherited policies.
        groups = self.iot.list_thing_groups_for_thing(thingName=thing)
        if groups.get("thingGroups") or groups.get("nextToken"):
            raise Rejected("thing_group_collision")
        return names == [thing] and policy_names == [self.policy] and ids == [arn]

    def _admit(self, key, inventory, arn):
        thing = inventory["thing"]
        attributes = {"onboardingOwner": inventory["owner"], "factoryFingerprint": inventory["cert_id"]}
        self._phase(key, "ENSURING_THING")
        # Adapter normalizes only ResourceNotFoundException to None.
        existing = self.iot.find_thing(thing)
        if existing is None:
            self.iot.create_thing(thingName=thing, attributePayload={"attributes": attributes})
            existing = self.iot.find_thing(thing)
        if existing is None or existing.get("attributes") != attributes or existing.get("thingTypeName"):
            raise Rejected("thing_ownership_collision")
        self._attachments(arn, thing)
        self._phase(key, "ATTACHING")
        self.iot.attach_thing_principal(thingName=thing, principal=arn,
                                        thingPrincipalType="EXCLUSIVE_THING")
        self.iot.attach_policy(policyName=self.policy, target=arn)
        if not self._attachments(arn, thing):
            raise RecoveryRequired("attachment_readback_incomplete")
        # These checks also prevent a direct administrative status change from
        # being silently overwritten. Out-of-band concurrent changes are forbidden.
        current = self.store.get(("inventory", inventory["owner"]))
        if current is None or current["data"] != inventory:
            raise RecoveryRequired("approval_changed")
        cert = self.iot.describe_certificate(certificateId=inventory["cert_id"])["certificateDescription"]
        if cert.get("status") != "PENDING_ACTIVATION" or cert.get("caCertificateId") != self.ca_id:
            raise RecoveryRequired("certificate_status_changed")
        self._phase(key, "ACTIVATING")
        self.iot.update_certificate(certificateId=inventory["cert_id"], newStatus="ACTIVE")
        cert = self.iot.describe_certificate(certificateId=inventory["cert_id"])["certificateDescription"]
        if cert.get("status") != "ACTIVE":
            raise RecoveryRequired("activation_readback_incomplete")
        self._phase(key, "BOOTSTRAP_ACTIVE")