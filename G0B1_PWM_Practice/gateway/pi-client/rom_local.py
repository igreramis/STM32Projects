"""Standalone local release selector. Offline unless --program is explicit.

No cloud, GPIO, reset, GO, retries, or changes to the trusted ROM engine.
Paths are relative to the current working directory, not the catalog location.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Dict

import rom_program as engine
from rom_read_id import ProbeError


VERSIONS = ("0.1.0", "0.2.0", "0.3.0")
LEGACY_BASELINE_SHA256 = "e46721beb7d9465aeb237c76f6dfa1bcfd584c4986578183bc7c66f582c9a5a5"
FLASH_SIZE = 524288
ERASE_SIZE = 20480
MAX_CATALOG_BYTES = 65536
SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class LocalRelease:
    path: Path
    size_bytes: int
    sha256: str
    expected_flash_sha256: str


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError("duplicate catalog key: " + key)
        result[key] = value
    return result


def _constant(value):
    raise ProbeError("non-finite JSON number is forbidden: " + value)


def _keys(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        raise ProbeError(label + " must contain exactly: " + ", ".join(expected))


def _hash(value, label):
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ProbeError(label + " must be a lowercase 64-character SHA-256")
    return value


def _read_bounded(path, limit):
    path = Path(path)
    if not path.is_file():
        raise ProbeError("not a regular file: " + str(path))
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ProbeError("file exceeds allowed size: " + str(path))
    return data


def validate_catalog(catalog_path, release_root, baseline_path) -> Dict[str, LocalRelease]:
    """Validate ALL releases against one pinned, full original backup.

    The catalog is local approval metadata, not a signature. It cannot replace
    the original baseline pin or authorize a different erase geometry.
    """
    if (engine.FLASH_BASE != 0x08000000 or engine.FLASH_SIZE != FLASH_SIZE or
            engine.PAGE_SIZE != 2048 or engine.PAGES != tuple(range(10)) or
            engine.ERASE_SIZE != ERASE_SIZE):
        raise ProbeError("ROM engine geometry differs from the approved layout")
    try:
        catalog = json.loads(_read_bounded(catalog_path, MAX_CATALOG_BYTES),
                             object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProbeError("invalid catalog JSON") from exc
    _keys(catalog, ("schema_version", "baseline_sha256", "releases"), "catalog")
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        raise ProbeError("schema_version must be integer 1")
    if _hash(catalog["baseline_sha256"], "baseline_sha256") != LEGACY_BASELINE_SHA256:
        raise ProbeError("catalog baseline is not the pinned original backup")
    _keys(catalog["releases"], VERSIONS, "releases")
    baseline = _read_bounded(baseline_path, FLASH_SIZE)
    if len(baseline) != FLASH_SIZE or engine.sha256(baseline) != LEGACY_BASELINE_SHA256:
        raise ProbeError("baseline must be the pinned original 524288-byte backup")

    root = Path(release_root).resolve()
    releases = {}
    seen = set()
    for version in VERSIONS:
        item = catalog["releases"][version]
        _keys(item, ("size_bytes", "sha256", "expected_flash_sha256"), version)
        size = item["size_bytes"]
        if type(size) is not int or not 8 <= size <= ERASE_SIZE:
            raise ProbeError(version + ": size must be 8..20480 with padding within pages 0-9")
        digest = _hash(item["sha256"], version + " sha256")
        expected = _hash(item["expected_flash_sha256"], version + " expected_flash_sha256")
        if digest in seen:
            raise ProbeError("duplicate release image SHA-256")
        seen.add(digest)
        path = (root / version / "firmware.bin").resolve()
        if root not in path.parents:
            raise ProbeError("release image escapes release root")
        image = _read_bounded(path, size)
        if len(image) != size or engine.sha256(image) != digest:
            raise ProbeError(version + ": image size/SHA-256 mismatch")
        engine.validate_vectors(image)
        computed = engine.sha256(image + b"\xff" * (ERASE_SIZE - size) + baseline[ERASE_SIZE:])
        if computed != expected:
            raise ProbeError(version + ": expected full-flash SHA-256 mismatch")
        releases[version] = LocalRelease(path, size, digest, computed)
    return releases


@contextmanager
def _configured_engine(release, predecessor_hash):
    """Single synchronous invocation; restore even on cancellation/failure.

    Only called after all catalog/predecessor validation. No phase changes,
    subclass overrides, readback overrides, or changes to protocol/geometry.
    """
    values = {
        "IMAGE_SIZE": release.size_bytes,
        "IMAGE_SHA256": release.sha256,
        "PADDED_SIZE": (release.size_bytes + 7) & ~7,
        "BASELINE_SHA256": predecessor_hash,
        "CONFIRMATION": "ERASE 0-9 WRITE " + release.sha256,
    }
    previous = {name: getattr(engine, name) for name in values}
    try:
        for name, value in values.items():
            setattr(engine, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(engine, name, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--version", required=True, choices=VERSIONS)
    parser.add_argument("--catalog", type=Path, default=Path("local-releases.json"))
    parser.add_argument("--release-root", type=Path, default=Path("local-releases"))
    parser.add_argument("--baseline", type=Path, default=Path("local-baseline.bin"))
    parser.add_argument("--program", action="store_true", help="enable supervised ROM engine session")
    parser.add_argument("--from-version", choices=("original",) + VERSIONS,
                        help="exact installed predecessor; REQUIRED with --program")
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--backup-root", type=Path, default=Path("rom-program-runs"))
    args = parser.parse_args(argv)
    if args.program and args.from_version is None:
        parser.error("--program requires explicit --from-version; never infer current flash")
    try:
        releases = validate_catalog(args.catalog, args.release_root, args.baseline)
        target = releases[args.version]
        predecessor = args.from_version or "original"
        before_hash = (LEGACY_BASELINE_SHA256 if predecessor == "original" else
                       releases[predecessor].expected_flash_sha256)
        print("Local target {} from {}; required backup SHA-256 {}".format(
            args.version, predecessor, before_hash), flush=True)
        print("Expected full-flash SHA-256: " + target.expected_flash_sha256, flush=True)
        padding = ((target.size_bytes + 7) & ~7) - target.size_bytes
        print("Actual write padding: {} FF bytes.".format(padding),
              flush=True)
        print("Stop the heartbeat monitor and all UART consoles before programming.", flush=True)
        forwarded = ["--image", str(target.path), "--port", args.port,
                     "--backup-root", str(args.backup_root)]
        if args.program:
            forwarded.append("--program")
        with _configured_engine(target, before_hash):
            # Engine revalidates selected bytes and owns them before any approval.
            return engine.main(forwarded)
    except (ProbeError, OSError) as exc:
        print("ERROR: {}. Stopped; no retry.".format(exc), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled/interrupted; no retry.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())