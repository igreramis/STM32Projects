"""Read-only cloud release check using the actual gateway verifier. Never installs."""
import base64
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization

from smoke_test import aws


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway/pi-client"))
import signed_release


def main():
    version, filename = sys.argv[1:]
    local = Path(filename).read_bytes()
    key_id = "arn:aws:kms:ap-southeast-2:844274146091:key/7ae64c36-9835-4346-bc18-42222602f31e"
    bucket, region = "iot-lab-firmware-844274146091", "ap-southeast-2"
    assert aws("sts", "get-caller-identity")["Account"] == "844274146091"
    stack = aws("cloudformation", "describe-stacks", "--stack-name", "stm32-admin-backend")["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    assert outputs["SigningKeyArn"] == key_id
    item = aws("dynamodb", "get-item", "--table-name", outputs["TableName"],
               "--consistent-read", "--key", json.dumps({
                   "pk": {"S": "release"}, "sk": {"S": version}}))["Item"]
    row = json.loads(item["data"]["S"])
    assert row["status"] == "approved"
    envelope = row["release"]
    key = aws("kms", "get-public-key", "--key-id", key_id)
    assert key["KeyId"] == key_id and key["KeySpec"] == "RSA_2048"
    assert key["KeyUsage"] == "SIGN_VERIFY"
    public = serialization.load_der_public_key(base64.b64decode(key["PublicKey"]))

    with tempfile.TemporaryDirectory(prefix="release-verification-") as directory:
        pem = Path(directory) / "public.pem"
        pem.write_bytes(public.public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo))
        # Verification runs locally with network and UART entry points blocked.
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("rom_program.open_port", side_effect=AssertionError("UART forbidden")):
            verifier = signed_release.ReleaseVerifier(pem, key_id, bucket, region)
            release = verifier.verify(envelope)
            assert release.version == version
            signed_release.validate_image(local, release)
            for field in ("payload", "signature"):
                tampered = dict(envelope)
                value = bytearray(base64.b64decode(tampered[field]))
                value[-1] ^= 1
                tampered[field] = base64.b64encode(value).decode()
                try:
                    verifier.verify(tampered)
                except ValueError as error:
                    assert str(error) == "invalid_signed_release"
                else:
                    raise AssertionError("Tampered " + field + " was accepted")
            altered_image = bytearray(local)
            altered_image[-1] ^= 1
            try:
                signed_release.validate_image(bytes(altered_image), release)
            except ValueError as error:
                assert str(error) == "invalid_signed_image"
            else:
                raise AssertionError("Tampered image was accepted")

        downloaded = Path(directory) / "downloaded.bin"
        metadata = aws("s3api", "get-object", "--bucket", bucket, "--key", release.key,
                       "--version-id", release.version_id, str(downloaded))
        assert metadata["VersionId"] == release.version_id
        assert downloaded.read_bytes() == local
        signed_release.validate_image(downloaded.read_bytes(), release)

    print(json.dumps({"verified_release": asdict(release), "signing_key": key_id,
                      "checks": ["gateway signature verification", "local image validation",
                                 "exact S3 version equals local binary", "tampered payload rejected",
                                 "tampered signature rejected", "tampered image rejected"],
                      "hardware_access": False, "deployment_published": False}, indent=2))


if __name__ == "__main__":
    main()