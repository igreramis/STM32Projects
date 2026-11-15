"""Offline signatures use an ephemeral test key, never KMS or credentials."""
import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import firmware
import signed_release as signed

KEY_ID = "arn:aws:kms:ap-southeast-2:123456789012:key/12345678-1234-1234-1234-123456789abc"


def key_fixture(root):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = root / "test-public.pem"
    path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo))
    verifier = signed.ReleaseVerifier(path, KEY_ID, "test-firmware-bucket", "ap-southeast-2")
    return key, verifier


def payload(version="7.8.9", image=None):
    return dict(schema=1, target="stm32g0b1", version=version,
                size_bytes=64 if image is None else len(image),
                sha256="a" * 64 if image is None else signed.rom_program.sha256(image),
                bucket="test-firmware-bucket", region="ap-southeast-2",
                key="firmware/stm32g0b1/" + version + "/firmware.bin", version_id="test-version+/=")


def envelope(key, value):
    raw = value if isinstance(value, bytes) else signed.canonical(value)
    signature = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    return dict(key_id=KEY_ID, payload=base64.b64encode(raw).decode("ascii"),
                signature=base64.b64encode(signature).decode("ascii"))


def url(value):
    return "https://{}.s3.{}.amazonaws.com/{}?{}".format(value["bucket"], value["region"], value["key"],
        urlencode({"versionId": value["version_id"], "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                   "X-Amz-Signature": "b" * 64}))


class SignedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key, cls.verifier = key_fixture(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_canonical_arbitrary_version(self):
        value = payload()
        release = self.verifier.verify(envelope(self.key, value))
        self.assertEqual(release.version, "7.8.9")
        self.assertEqual(release.target, "stm32g0b1")

    def test_signature_before_parse(self):
        bad = envelope(self.key, b"not json")
        bad["signature"] = base64.b64encode(b"x" * 256).decode()
        with patch.object(signed.json, "loads", side_effect=AssertionError("parsed before verification")) as parse:
            with self.assertRaisesRegex(ValueError, "invalid_signed_release"):
                self.verifier.verify(bad)
            parse.assert_not_called()

    def test_envelope_and_signature_validation(self):
        good = envelope(self.key, payload())
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad_values = [dict(good, key_id=KEY_ID + "x"), dict(good, extra=1),
                      dict(good, payload="%%%"), dict(good, payload="a" * 4097),
                      dict(good, signature="a" * 344), dict(good, signature=""),
                      dict(good, payload=base64.b64encode(b"{}").decode()), envelope(other, payload()), None]
        for field in good:
            bad_values.append({k: v for k, v in good.items() if k != field})
        for value in bad_values:
            with self.subTest(value=str(value)[:50]), self.assertRaises(ValueError):
                self.verifier.verify(value)

    def test_exact_fields_types_and_locations(self):
        good = payload()
        invalid = {"schema": [True, 1.0, 2], "target": ["NUCLEO-G0B1RE", None],
            "version": ["01.2.3", "1.2", "1.2.3-rc", "1.2.3\n", 123, "9999999999.0.0"],
            "size_bytes": [True, 7, 20481, 64.0], "sha256": ["A" * 64, "a" * 63, None],
            "bucket": ["other-bucket"], "region": ["us-east-1"], "key": ["../firmware.bin", "firmware/stm32g0b1/1.0.0/firmware.bin"],
            "version_id": ["null", "", "a\n", "a" * 1025, None]}
        values = [dict(good, extra=1)] + [{k: v for k, v in good.items() if k != field} for field in good]
        values += [dict(good, **{field: bad}) for field, bads in invalid.items() for bad in bads]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.verifier.verify(envelope(self.key, value))

    def test_noncanonical_duplicate_and_nonfinite_json(self):
        values = [json.dumps(payload()).encode(), b'{"schema":1,"schema":1}', b"NaN", b"[]", b"\xff",
                  signed.canonical(payload()) + b"\n", b"[" * 1100]
        for value in values:
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                self.verifier.verify(envelope(self.key, value))

    def test_exact_url_and_command_binding(self):
        value = payload()
        command = dict(command_id="test", version=value["version"], sha256=value["sha256"],
                       release=envelope(self.key, value), url=url(value))
        self.assertEqual(self.verifier.command(command).version, value["version"])
        urls = [command["url"].replace("https:", "http:"), command["url"] + "#fragment",
                command["url"].replace("test-firmware-bucket", "evil-bucket"),
                command["url"].replace("7.8.9/", "7.8.8/"),
                command["url"].replace("versionId=", "wrong="),
                command["url"] + "&versionId=other", command["url"].replace("versionId=test", "versionId=wrong"),
                command["url"].replace(".com/", ".com:443/"), command["url"].replace("https://", "https://user@")]
        for bad in urls:
            with self.subTest(url=bad), self.assertRaises((ValueError, firmware.StageError)):
                self.verifier.command(dict(command, url=bad))
        for changes in ({"version": "7.8.8"}, {"sha256": "c" * 64}):
            with self.assertRaises(ValueError):
                self.verifier.command(dict(command, **changes))

    def test_binary_version_boundaries_and_vectors(self):
        image = (0x20024000).to_bytes(4, "little") + (0x08000009).to_bytes(4, "little") + b"firmware=7.8.9\x00"
        release = self.verifier.verify(envelope(self.key, payload(image=image)))
        signed.validate_image(image, release)
        for bad in [image[:7], image.replace(b"7.8.9", b"7.8.90"), image.replace(b"7.8.9", b"7.8.9-rc"),
                    image[:4] + (0x08000001).to_bytes(4, "little") + image[8:],
                    b"\x00" * 4 + image[4:], image + b"firmware=1.0.0\x00"]:
            metadata = replace(release, size_bytes=len(bad), sha256=signed.rom_program.sha256(bad))
            with self.assertRaises(Exception):
                signed.validate_image(bad, metadata)

    def test_key_configuration_requires_rsa2048(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "key.pem"
            key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
            path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                         serialization.PublicFormat.SubjectPublicKeyInfo))
            with self.assertRaises(ValueError):
                signed.ReleaseVerifier(path, KEY_ID, "test-firmware-bucket", "ap-southeast-2")

    def test_pss_and_wrong_digest_are_not_kms_pkcs1_sha256(self):
        raw = signed.canonical(payload())
        good = envelope(self.key, raw)
        for scheme, algorithm in ((padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                               salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256()),
                                  (padding.PKCS1v15(), hashes.SHA384())):
            signature = self.key.sign(raw, scheme, algorithm)
            with self.assertRaisesRegex(ValueError, "invalid_signed_release"):
                self.verifier.verify(dict(good, signature=base64.b64encode(signature).decode()))

    def test_valid_payload_tampering_without_resigning_is_rejected(self):
        good = envelope(self.key, payload())
        changed = signed.canonical(dict(payload(), size_bytes=65))
        with self.assertRaisesRegex(ValueError, "invalid_signed_release"):
            self.verifier.verify(dict(good, payload=base64.b64encode(changed).decode()))

    def test_signer_configuration_requires_exact_arn_region_and_local_pem(self):
        with tempfile.TemporaryDirectory() as root:
            _, verifier = key_fixture(Path(root))
            path = Path(root) / "test-public.pem"
            for arn, bucket, region in (("alias/release", verifier.bucket, verifier.region),
                                       (KEY_ID, verifier.bucket, "us-east-1"),
                                       (KEY_ID, "https://evil.invalid", verifier.region)):
                with self.assertRaises(ValueError):
                    signed.ReleaseVerifier(path, arn, bucket, region)
            path.write_bytes(b"x" * 8193)
            with self.assertRaises(ValueError):
                signed.ReleaseVerifier(path, KEY_ID, verifier.bucket, verifier.region)