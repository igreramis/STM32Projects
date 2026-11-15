"""Offline KMS RSA-2048 release verification. Trust is provisioned locally only."""

import base64
import json
from pathlib import Path
import re

import firmware
import rom_program

SEMVER = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\Z")
FIELDS = {"schema", "target", "version", "size_bytes", "sha256", "bucket",
          "region", "key", "version_id"}


def version(value):
    return type(value) is str and SEMVER.fullmatch(value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_release")
        result[key] = value
    return result


class ReleaseVerifier:
    def __init__(self, public_key, key_id, bucket, region):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        if (type(key_id) is not str or re.fullmatch(
                r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[0-9a-f-]{36}", key_id) is None or
                type(bucket) is not str or re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is None or
                type(region) is not str or re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region) is None or
                key_id.split(":")[3] != region):
            raise ValueError("invalid_signing_configuration")
        with Path(public_key).open("rb") as source:
            pem = source.read(8193)
        if len(pem) > 8192:
            raise ValueError("invalid_signing_configuration")
        self.key = serialization.load_pem_public_key(pem)
        if not isinstance(self.key, rsa.RSAPublicKey) or self.key.key_size != 2048:
            raise ValueError("invalid_signing_configuration")
        self.key_id, self.bucket, self.region = key_id, bucket, region

    def verify(self, envelope):
        """Verify ORIGINAL bytes before JSON parsing; no key lookup or network."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            if (type(envelope) is not dict or set(envelope) != {"key_id", "payload", "signature"} or
                    envelope["key_id"] != self.key_id or
                    type(envelope["payload"]) is not str or not 1 <= len(envelope["payload"]) <= 4096 or
                    type(envelope["signature"]) is not str or len(envelope["signature"]) != 344):
                raise ValueError()
            payload = base64.b64decode(envelope["payload"], validate=True)
            signature = base64.b64decode(envelope["signature"], validate=True)
            if len(signature) != 256:
                raise ValueError()
            self.key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
            data = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
            if (type(data) is not dict or set(data) != FIELDS or canonical(data) != payload or
                    type(data["schema"]) is not int or data["schema"] != 1 or
                    data["target"] != "stm32g0b1" or not version(data["version"]) or
                    type(data["size_bytes"]) is not int or not 8 <= data["size_bytes"] <= 20480 or
                    type(data["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", data["sha256"]) is None or
                    data["bucket"] != self.bucket or data["region"] != self.region or
                    data["key"] != "firmware/stm32g0b1/" + data["version"] + "/firmware.bin" or
                    type(data["version_id"]) is not str or data["version_id"] == "null" or
                    re.fullmatch(r"[A-Za-z0-9_+/=.\-]{1,1024}", data["version_id"]) is None):
                raise ValueError()
            return firmware.Release(**{key: value for key, value in data.items() if key != "schema"})
        except Exception:
            raise ValueError("invalid_signed_release") from None

    def command(self, command):
        release = self.verify(command["release"])
        if (command["version"] != release.version or command["sha256"] != release.sha256):
            raise ValueError("invalid_signed_release")
        # Reuse the HTTPS-only, no-redirect, exact object-version URL policy.
        firmware.FirmwareStager(release, ".").validate(dict(
            command_id=command["command_id"], command="stage_firmware",
            version=command["version"], url=command["url"]))
        return release


def validate_image(image, release):
    if len(image) != release.size_bytes or rom_program.sha256(image) != release.sha256:
        raise ValueError("invalid_signed_image")
    rom_program.validate_vectors(image)
    reset = int.from_bytes(image[4:8], "little") & ~1
    if reset < rom_program.FLASH_BASE + 8:
        raise ValueError("invalid_signed_image")
    tokens = re.findall(rb"firmware=([^\x00\s]+)", image)
    if not tokens or any(token != release.version.encode("ascii") for token in tokens):
        raise ValueError("invalid_signed_image")