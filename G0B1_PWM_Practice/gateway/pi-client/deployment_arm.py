"""Offline deployment permission; admission time and consumption belong to OtaJobs."""

import hashlib
import re
import uuid

from signed_release import canonical, version


FIELDS = {"schema", "purpose", "command_id", "gateway_id", "device_id", "version",
          "from_version", "sha256", "release_sha256", "issued_at", "arm_expires_at", "expires_at"}
DEVICE_ID = "stm32g0b1-01"
ARM_SECONDS = 120
COMMAND_SECONDS = 900
# Only the future-issued boundary has tolerance; admission expiry is exclusive.
ISSUED_SKEW_SECONDS = 1


class DeploymentArmVerifier:
    """Use the locally pinned release signer, never a key named by the command.

    Returns authenticated/bound fields, without enforcing admission time. This
    lets the coordinator journal authentic expired permits as terminal rejects.
    No release URL, UART, download, clock, or persistent state is touched here.
    """

    def __init__(self, release_verifier, gateway_id):
        self.release_verifier = release_verifier
        self.gateway_id = gateway_id

    def __call__(self, command):
        try:
            data = self.release_verifier._payload(command["deployment_arm"])
            if (set(data) != FIELDS or type(data["schema"]) is not int or data["schema"] != 1 or
                    any(type(data[key]) is not str for key in FIELDS -
                        {"schema", "issued_at", "arm_expires_at", "expires_at"}) or
                    data["purpose"] != "deploy_firmware" or
                    str(uuid.UUID(data["command_id"])) != data["command_id"] or
                    data["gateway_id"] != self.gateway_id or data["device_id"] != DEVICE_ID or
                    not version(data["version"]) or not version(data["from_version"]) or
                    any(re.fullmatch(r"[0-9a-f]{64}", data[key]) is None
                        for key in ("sha256", "release_sha256")) or
                    any(type(data[key]) is not int or data[key] < 0
                        for key in ("issued_at", "arm_expires_at", "expires_at")) or
                    data["arm_expires_at"] != data["issued_at"] + ARM_SECONDS or
                    data["expires_at"] != data["issued_at"] + COMMAND_SECONDS or
                    any(type(command[key]) is not type(data[key]) or command[key] != data[key]
                        for key in ("command_id", "version", "from_version", "sha256", "expires_at")) or
                    data["release_sha256"] != hashlib.sha256(canonical(command["release"])).hexdigest()):
                raise ValueError()
            return data
        except Exception:
            raise ValueError("invalid_deployment_arm") from None