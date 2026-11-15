"""Offline fixtures and a simulated ROM only; never open a hardware serial port.

Synthetic backups use a test-only pin patch; production keeps the original pin.
No real catalog, release binaries, baseline, credentials, or cloud calls required.
"""

import builtins
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import rom_local as local
import rom_program as engine
from rom_read_id import ProbeError
from test_rom_program import MemoryRom


PIN = "e46721beb7d9465aeb237c76f6dfa1bcfd584c4986578183bc7c66f582c9a5a5"
GLOBAL_NAMES = ("IMAGE_SIZE", "IMAGE_SHA256", "PADDED_SIZE", "BASELINE_SHA256", "CONFIRMATION")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def image_bytes(size, marker=0x12, sp=0x20024000, reset=0x08000009):
    return sp.to_bytes(4, "little") + reset.to_bytes(4, "little") + bytes([marker]) * (size - 8)


class PinTests(unittest.TestCase):
    def test_original_pin_and_geometry_are_fixed(self):
        self.assertEqual(local.LEGACY_BASELINE_SHA256, PIN)
        self.assertEqual(local.FLASH_SIZE, 512 * 1024)
        self.assertEqual(local.ERASE_SIZE, 20480)
        self.assertEqual(local.VERSIONS, ("0.1.0", "0.2.0", "0.3.0"))


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog_path = self.root / "catalog.json"
        self.release_root = self.root / "releases"
        self.baseline_path = self.root / "baseline.bin"
        self.baseline = bytes(range(256)) * 2048
        self.baseline_path.write_bytes(self.baseline)
        pin = patch.object(local, "LEGACY_BASELINE_SHA256", digest(self.baseline))
        pin.start()
        self.addCleanup(pin.stop)
        self.catalog = {"schema_version": 1, "baseline_sha256": digest(self.baseline), "releases": {}}
        self.images = {}
        for number, version in enumerate(local.VERSIONS):
            self.set_image(version, image_bytes(33 + number * 8, 20 + number))
        self.save_catalog()
        self.output = io.StringIO()
        for redirect in (contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output)):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)
        self.original_globals = self.globals()
        self.addCleanup(lambda: self.assertEqual(self.original_globals, self.globals()))

    def globals(self):
        return {name: getattr(engine, name) for name in GLOBAL_NAMES}

    def full_flash(self, image):
        return image + b"\xff" * (20480 - len(image)) + self.baseline[20480:]

    def set_image(self, version, data):
        path = self.release_root / version / "firmware.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.images[version] = data
        self.catalog["releases"][version] = {
            "size_bytes": len(data), "sha256": digest(data),
            "expected_flash_sha256": digest(self.full_flash(data)),
        }

    def save_catalog(self):
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")

    def validate(self):
        return local.validate_catalog(self.catalog_path, self.release_root, self.baseline_path)

    def argv(self, *extra):
        return ["--version", "0.2.0", "--catalog", str(self.catalog_path),
                "--release-root", str(self.release_root), "--baseline", str(self.baseline_path), *extra]

    def assert_rejected(self):
        with patch.object(engine, "main") as invoke, patch.object(engine, "open_port") as port:
            self.assertEqual(local.main(self.argv()), 1)
            invoke.assert_not_called()
            port.assert_not_called()
        self.assertEqual(self.globals(), self.original_globals)


class CatalogTests(Fixture):
    def test_all_three_releases_and_full_hashes(self):
        releases = self.validate()
        self.assertEqual(tuple(releases), local.VERSIONS)
        for version, release in releases.items():
            self.assertEqual(release.path, self.release_root / version / "firmware.bin")
            self.assertEqual(release.sha256, digest(self.images[version]))
            self.assertEqual(release.expected_flash_sha256, digest(self.full_flash(self.images[version])))

    def test_malformed_json(self):
        for raw in (b"{", b"\xff", b"[]", b"null", b"NaN", b"Infinity", b"1e999",
                    b'{"schema_version":1,"schema_version":1}', b"[" * 1100):
            with self.subTest(raw=raw[:40]):
                self.catalog_path.write_bytes(raw)
                self.assert_rejected()

    def test_duplicate_nested_keys(self):
        raw = json.dumps(self.catalog).replace('"size_bytes": 33', '"size_bytes": 33, "size_bytes": 33')
        self.catalog_path.write_text(raw)
        self.assert_rejected()

    def test_schema_and_exact_fields(self):
        original = json.dumps(self.catalog)
        for value in (True, False, 0, 2, "1", 1.0, None):
            self.catalog = json.loads(original)
            self.catalog["schema_version"] = value
            self.save_catalog()
            self.assert_rejected()
        for field in ("schema_version", "baseline_sha256", "releases"):
            self.catalog = json.loads(original)
            del self.catalog[field]
            self.save_catalog()
            self.assert_rejected()
        self.catalog = json.loads(original)
        self.catalog["arbitrary_hash"] = "0" * 64
        self.save_catalog()
        self.assert_rejected()

    def test_release_mapping_requires_exact_three_versions(self):
        original = json.dumps(self.catalog)
        for version in local.VERSIONS:
            self.catalog = json.loads(original)
            del self.catalog["releases"][version]
            self.save_catalog()
            self.assert_rejected()
        for value in ([], None, {"0.4.0": {}}, dict(self.catalog["releases"], extra={})):
            self.catalog = json.loads(original)
            self.catalog["releases"] = value
            self.save_catalog()
            self.assert_rejected()

    def test_release_metadata_exact_fields(self):
        original = json.dumps(self.catalog)
        for item in (None, [], {}, {"size_bytes": 33},
                     dict(self.catalog["releases"]["0.1.0"], extra=True)):
            self.catalog = json.loads(original)
            self.catalog["releases"]["0.1.0"] = item
            self.save_catalog()
            self.assert_rejected()

    def test_hash_formats_rejected(self):
        original = json.dumps(self.catalog)
        for key in ("sha256", "expected_flash_sha256"):
            for value in (None, 1, [], "A" * 64, "g" * 64, "0" * 63, "0" * 65, "0" * 64 + "\n"):
                self.catalog = json.loads(original)
                self.catalog["releases"]["0.3.0"][key] = value
                self.save_catalog()
                self.assert_rejected()

    def test_catalog_cannot_select_another_baseline(self):
        self.catalog["baseline_sha256"] = "0" * 64
        self.save_catalog()
        self.assert_rejected()

    def test_synthetic_baseline_fails_real_production_pin(self):
        self.catalog["baseline_sha256"] = PIN
        self.save_catalog()
        with patch.object(local, "LEGACY_BASELINE_SHA256", PIN):
            self.assert_rejected()

    def test_baseline_size_and_hash(self):
        for data in (b"", self.baseline[:-1], self.baseline + b"x", b"x" + self.baseline[1:]):
            with self.subTest(size=len(data)):
                self.baseline_path.write_bytes(data)
                self.assert_rejected()

    def test_catalog_size_limit(self):
        self.catalog_path.write_bytes(b" " * (local.MAX_CATALOG_BYTES + 1))
        self.assert_rejected()

    def test_missing_files_and_directory_instead_of_file(self):
        for path in (self.catalog_path, self.baseline_path, self.release_root / "0.3.0" / "firmware.bin"):
            content = path.read_bytes()
            path.unlink()
            self.assert_rejected()
            path.mkdir()
            self.assert_rejected()
            path.rmdir()
            path.write_bytes(content)

    def test_size_metadata(self):
        for value in (True, "33", 33.0, None, -1, 0, 7, 20481, 524288):
            self.catalog["releases"]["0.3.0"]["size_bytes"] = value
            self.save_catalog()
            self.assert_rejected()

    def test_file_size_and_hash_including_unselected_release(self):
        path = self.release_root / "0.3.0" / "firmware.bin"
        original = self.images["0.3.0"]
        for data in (original[:-1], original + b"x", original[:-1] + b"x", image_bytes(20481)):
            path.write_bytes(data)
            self.assert_rejected()

    def test_maximum_size_and_alignment_padding(self):
        for size in (20473, 20479, 20480):
            self.set_image("0.3.0", image_bytes(size))
            self.save_catalog()
            release = self.validate()["0.3.0"]
            with local._configured_engine(release, digest(self.baseline)):
                self.assertEqual(engine.PADDED_SIZE, 20480)
                self.assertEqual(engine.load_image(release.path), self.images["0.3.0"])

    def test_vectors_even_with_matching_hashes(self):
        for sp, reset in ((0x20000000, 0x08000009), (0x20024008, 0x08000009),
                          (0x20000001, 0x08000009), (0x20024000, 0x08000008),
                          (0x20024000, 0x08000041), (0x20024000, 0x07000001)):
            self.set_image("0.3.0", image_bytes(64, sp=sp, reset=reset))
            self.save_catalog()
            self.assert_rejected()

    def test_duplicate_image_hashes(self):
        self.set_image("0.3.0", self.images["0.1.0"])
        self.save_catalog()
        self.assert_rejected()

    def test_expected_full_flash_hash_recomputed(self):
        self.catalog["releases"]["0.3.0"]["expected_flash_sha256"] = "0" * 64
        self.save_catalog()
        self.assert_rejected()

    def test_baseline_tail_used_not_all_ff(self):
        image = self.images["0.3.0"]
        self.catalog["releases"]["0.3.0"]["expected_flash_sha256"] = digest(
            image + b"\xff" * (524288 - len(image)))
        self.save_catalog()
        self.assert_rejected()

    def test_symlink_escape_rejected(self):
        path = self.release_root / "0.3.0" / "firmware.bin"
        outside = self.root / "outside.bin"
        path.rename(outside)
        path.symlink_to(outside)
        self.assert_rejected()

    def test_engine_geometry_drift_rejected(self):
        for name, value in (("FLASH_BASE", 0), ("FLASH_SIZE", 1), ("PAGE_SIZE", 4096),
                            ("PAGES", tuple(range(11))), ("ERASE_SIZE", 22528)):
            with patch.object(engine, name, value):
                self.assert_rejected()


class WrapperTests(Fixture):
    def test_cli_rejects_unknown_missing_from_version_and_hash_override(self):
        for extra in (("--program",), ("--from-version", "unknown"),
                      ("--version", "0.4.0"), ("--sha256", "0" * 64),
                      ("--baseline-sha256", PIN), ("--image", "other.bin"), ("--prog",)):
            with self.subTest(extra=extra), patch.object(engine, "main") as invoke:
                with self.assertRaises(SystemExit) as error:
                    local.main(self.argv(*extra))
                self.assertEqual(error.exception.code, 2)
                invoke.assert_not_called()
        with self.assertRaises(SystemExit) as error:
            local.main([])
        self.assertEqual(error.exception.code, 2)

    def test_program_requires_from_version_before_catalog_read(self):
        with patch.object(local, "validate_catalog") as validate:
            with self.assertRaises(SystemExit):
                local.main(self.argv("--program"))
            validate.assert_not_called()

    def test_all_target_predecessor_globals_and_arguments(self):
        for version in local.VERSIONS:
            for predecessor in ("original",) + local.VERSIONS:
                target = self.catalog["releases"][version]
                expected_before = (digest(self.baseline) if predecessor == "original" else
                                   self.catalog["releases"][predecessor]["expected_flash_sha256"])

                def invoke(argv):
                    self.assertEqual(self.globals(), {
                        "IMAGE_SIZE": target["size_bytes"], "IMAGE_SHA256": target["sha256"],
                        "PADDED_SIZE": (target["size_bytes"] + 7) & ~7,
                        "BASELINE_SHA256": expected_before,
                        "CONFIRMATION": "ERASE 0-9 WRITE " + target["sha256"],
                    })
                    self.assertEqual(argv, ["--image", str(self.release_root / version / "firmware.bin"),
                                           "--port", "/dev/ttyAMA0", "--backup-root", str(self.root / "runs"),
                                           "--program"])
                    return 2

                with self.subTest(version=version, predecessor=predecessor), patch.object(engine, "main", invoke):
                    self.assertEqual(local.main(self.argv("--version", version, "--program", "--from-version",
                                                         predecessor, "--port", "/dev/ttyAMA0", "--backup-root",
                                                         str(self.root / "runs"))), 2)
                self.assertEqual(self.globals(), self.original_globals)

    def test_globals_restored_on_engine_errors_and_interrupts(self):
        for error, result in ((ProbeError("test"), 1), (OSError("test"), 1),
                              (KeyboardInterrupt(), 130), (EOFError(), 130)):
            with patch.object(engine, "main", side_effect=error):
                self.assertEqual(local.main(self.argv()), result)
            self.assertEqual(self.globals(), self.original_globals)
        for error in (RuntimeError("test"), SystemExit(7)):
            with patch.object(engine, "main", side_effect=error), self.assertRaises(type(error)):
                local.main(self.argv())
            self.assertEqual(self.globals(), self.original_globals)

    def test_real_engine_offline_never_imports_serial_or_opens_port(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "serial" or name.startswith("serial."):
                self.fail("offline invocation imported serial")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import), \
                patch.object(engine, "open_port") as port, patch.object(engine, "RunRecord") as record, \
                patch("builtins.input") as prompt:
            self.assertEqual(local.main(self.argv()), 0)
            port.assert_not_called()
            record.assert_not_called()
            prompt.assert_not_called()
        self.assertEqual(self.globals(), self.original_globals)

    def test_default_paths_and_no_program_flag(self):
        releases = self.validate()
        with patch.object(local, "validate_catalog", return_value=releases) as validate, \
                patch.object(engine, "main", return_value=0) as invoke:
            self.assertEqual(local.main(["--version", "0.1.0"]), 0)
            validate.assert_called_once_with(Path("local-releases.json"), Path("local-releases"),
                                             Path("local-baseline.bin"))
            self.assertNotIn("--program", invoke.call_args[0][0])

    def test_selected_file_change_is_revalidated_before_serial(self):
        validate = local.validate_catalog

        def change_after_validation(*args):
            releases = validate(*args)
            releases["0.2.0"].path.write_bytes(b"changed")
            return releases

        with patch.object(local, "validate_catalog", side_effect=change_after_validation), \
                patch.object(engine, "open_port") as port, patch.object(engine, "RunRecord") as record:
            self.assertEqual(local.main(self.argv("--program", "--from-version", "original")), 1)
            port.assert_not_called()
            record.assert_not_called()

    def run_simulated(self, before, answers):
        rom = MemoryRom()
        rom.memory = bytearray(before)
        rom.reset_input_buffer = Mock()
        with patch.object(engine.sys.stdin, "isatty", return_value=True), \
                patch.object(engine, "open_port", return_value=contextlib.nullcontext(rom)), \
                patch("builtins.input", side_effect=answers) as prompt:
            result = local.main(self.argv("--program", "--from-version", "0.1.0", "--port", "/dev/ttyAMA0",
                                          "--backup-root", str(self.root / "runs")))
        return result, rom, prompt

    def test_simulated_program_from_known_release_preserves_tail(self):
        before = self.full_flash(self.images["0.1.0"])
        phrase = "ERASE 0-9 WRITE " + digest(self.images["0.2.0"])
        result, rom, prompt = self.run_simulated(before, ["READY", "RESET", phrase])
        self.assertEqual(result, 0)
        self.assertEqual(bytes(rom.memory), self.full_flash(self.images["0.2.0"]))
        self.assertEqual(rom.erased_pages, list(range(10)))
        self.assertEqual(sum(rom.write_sizes), 48)
        self.assertEqual(prompt.call_count, 3)
        self.assertIn(phrase, prompt.call_args[0][0])
        backups = list((self.root / "runs").glob("*/flash-before.bin"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(self.globals(), self.original_globals)

    def test_wrong_predecessor_backup_blocks_erase_confirmation(self):
        result, rom, prompt = self.run_simulated(self.baseline, ["READY", "RESET"])
        self.assertEqual(result, 1)
        self.assertEqual(prompt.call_count, 2)
        self.assertNotIn(0x44, rom.commands)
        self.assertNotIn(0x31, rom.commands)
        self.assertEqual(bytes(rom.memory), self.baseline)

    def test_ready_reset_and_exact_hash_phrase_gates(self):
        before = self.full_flash(self.images["0.1.0"])
        for answers in (["no"], ["READY", "no"], ["READY", "RESET", "ERASE 0-9 WRITE wrong"]):
            result, rom, prompt = self.run_simulated(before, answers)
            self.assertEqual(result, 2)
            self.assertEqual(prompt.call_count, len(answers))
            self.assertNotIn(0x44, rom.commands)
            self.assertNotIn(0x31, rom.commands)
            self.assertEqual(bytes(rom.memory), before)
            if len(answers) < 3:
                self.assertEqual(rom.packets, [])


if __name__ == "__main__":
    unittest.main()