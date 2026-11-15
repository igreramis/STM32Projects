"""Package explicitly requested local lab releases; never upload or flash."""
import hashlib
import json
from pathlib import Path
import shutil

WORKSPACE = Path(__file__).resolve().parents[1]
ROOT = Path.home() / "Downloads/resumes/plasmaleap/aws/releases/stm32g0b1"
VERSIONS = ("0.1.0", "0.2.0", "0.3.0")
ORIGINAL_IMAGE = "e2c6d4bcf172cc803fa497a60bd0b625f5448a33433ab2d00bacec5fff6cc521"
ORIGINAL_FLASH = "e46721beb7d9465aeb237c76f6dfa1bcfd584c4986578183bc7c66f582c9a5a5"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    baseline = (WORKSPACE / "build/boot-option-backup/flash-before.bin").read_bytes()
    assert len(baseline) == 524288 and digest(baseline) == ORIGINAL_FLASH
    images = {}
    for version in VERSIONS:
        image = (WORKSPACE / "build/Debug/versions" / version / "firmware.bin").read_bytes()
        assert 8 <= len(image) <= 20480
        assert (b"Hello from NUCLEO-G0B1RE! firmware=" + version.encode() + b"\r\n\0") in image
        sp, reset = int.from_bytes(image[:4], "little"), int.from_bytes(image[4:8], "little")
        assert sp % 8 == 0 and 0x20000000 < sp <= 0x20024000
        assert reset & 1 and 0x08000000 <= reset & ~1 < 0x08000000 + len(image)
        images[version] = image
    assert len({digest(data) for data in images.values()}) == 3
    # Refuse unknown existing files; preserve original artifact+metadata before replacement.
    for version, image in images.items():
        existing = ROOT / version / "firmware.bin"
        if existing.exists():
            allowed = {digest(image)} | ({ORIGINAL_IMAGE} if version == "0.1.0" else set())
            assert digest(existing.read_bytes()) in allowed, "Unexpected release contents"
    original = ROOT / "0.1.0"
    archive = ROOT / "archive" / "0.1.0-original-unversioned"
    if digest((original / "firmware.bin").read_bytes()) == ORIGINAL_IMAGE:
        if archive.exists():
            assert digest((archive / "firmware.bin").read_bytes()) == ORIGINAL_IMAGE
        else:
            shutil.copytree(original, archive)
        assert digest((archive / "firmware.bin").read_bytes()) == ORIGINAL_IMAGE
    catalog = {"schema_version": 1, "baseline_sha256": ORIGINAL_FLASH, "releases": {}}
    for version, image in images.items():
        dest = ROOT / version
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("firmware.bin", "firmware.elf", "firmware.map"):
            shutil.copy2(WORKSPACE / "build/Debug/versions" / version / name, dest / name)
        assert (dest / "firmware.bin").read_bytes() == image
        entry = {"size_bytes": len(image), "sha256": digest(image),
                 "expected_flash_sha256": digest(image + b"\xff" * (20480 - len(image)) + baseline[20480:])}
        catalog["releases"][version] = entry
        manifest = {"schema_version": 1, "target": "NUCLEO-G0B1RE", "mcu": "STM32G0B1RETx",
                    "version": version, "build_type": "Debug", "format": "bin", "filename": "firmware.bin",
                    "flash_address": "0x08000000", "local_only": True,
                    "heartbeat": "Hello from NUCLEO-G0B1RE! firmware=" + version,
                    "heartbeat_uarts": "USART2 PA2 and USART1 PA9, 115200 8N1, every 10 seconds", **entry}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (dest / "README.md").write_text(
            "# Local lab firmware " + version + "\n\n"
            "Heartbeat embeds firmware=" + version + ". Mirrored to ST-LINK USART2 and Pi USART1 TX/PA9.\n"
            "115200 8N1, every 10 seconds. Factory ROM programming remains 8E1.\n"
            "Built from the same source/Debug flags; only APP_VERSION differs.\n"
            "Local-only replacement: NOT the previously published cloud 0.1.0 image.\n"
            "Original 0.1.0 preserved under ../archive/0.1.0-original-unversioned/.\n"
            "Manifest is unsigned integrity metadata, not publisher authentication.\n"
            "Do not upload over existing cloud release metadata. Hardware tests tracked separately.\n")
        print(version, json.dumps(entry))
    (ROOT / "local-releases.json").write_text(json.dumps(catalog, indent=2) + "\n")
    print("Original release preserved:", archive)


if __name__ == "__main__":
    main()