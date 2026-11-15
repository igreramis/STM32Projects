"""Durable predecessor tests using only a synthetic validated local catalog."""
import json
from pathlib import Path
from unittest.mock import patch

import rom_local
from test_rom_local import Fixture
from verified_state import VerifiedState


class VerifiedStateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.releases = self.validate()
        self.state = VerifiedState(self.root / "verified", "0.1.0")

    def test_seed_only_from_validated_catalog_and_private_durable_files(self):
        value = self.state.load(self.releases)
        self.assertEqual(value["flash_sha256"], self.releases["0.1.0"].expected_flash_sha256)
        self.assertEqual(value["baseline_sha256"], rom_local.LEGACY_BASELINE_SHA256)
        self.assertEqual(self.state.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.state.root.glob(".state-*")), [])

    def test_original_seed_is_fixed_not_supplied_flash(self):
        self.state.seed_version = "original"
        self.assertEqual(self.state.load(self.releases)["flash_sha256"], rom_local.LEGACY_BASELINE_SHA256)

    def test_existing_state_ignores_changed_seed(self):
        expected = self.state.load(self.releases)
        restarted = VerifiedState(self.state.root, "original")
        self.assertEqual(restarted.load(self.releases), expected)

    def test_intent_survives_restart_and_cannot_be_reseeded(self):
        self.state.intent(self.state.load(self.releases))
        with self.assertRaises(ValueError):
            VerifiedState(self.state.root, "original").load(self.releases)

    def test_removed_directory_does_not_reseed_after_initialization(self):
        self.state.load(self.releases)
        self.state.path.unlink()
        self.state.root.rmdir()
        with self.assertRaises(ValueError):
            VerifiedState(self.state.root, "original").load(self.releases)

    def test_existing_empty_directory_is_not_initial_state(self):
        self.state.root.mkdir()
        with self.assertRaises(ValueError):
            self.state.load(self.releases)

    def test_reject_corrupt_schema_types_hashes_and_unknown_fields(self):
        good = self.state.load(self.releases)
        changes = [dict(schema=True), dict(schema=2), dict(status="pending"),
                   dict(version="01.2.3"), dict(flash_sha256="A" * 64),
                   dict(flash_sha256="0" * 64), dict(baseline_sha256="0" * 64), dict(extra=1)]
        values = [json.dumps(dict(good, **change)) for change in changes]
        values += ["{}", "[]", "not JSON", json.dumps(good)[:-1] + ',"schema":1}', " " * 2049]
        for raw in values:
            with self.subTest(raw=raw[:60]):
                self.state.path.write_text(raw)
                with self.assertRaises(Exception):
                    self.state.load(self.releases)

    def test_state_and_root_symlinks_fail_closed(self):
        self.state.load(self.releases)
        other = self.root / "other.json"
        self.state.path.rename(other)
        self.state.path.symlink_to(other)
        with self.assertRaises(ValueError):
            self.state.load(self.releases)
        alias = self.root / "alias"
        alias.symlink_to(self.state.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            VerifiedState(alias, "original").load(self.releases)

    def test_initial_write_crash_never_adopts_on_restart(self):
        with patch.object(self.state, "_write", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.state.load(self.releases)
        with self.assertRaises(ValueError):
            VerifiedState(self.state.root, "original").load(self.releases)

    def test_dynamic_verified_hash_round_trip(self):
        previous = self.state.load(self.releases)
        self.state.intent(previous)
        self.state.verified("12.34.56", "a" * 64)
        loaded = VerifiedState(self.state.root, "original").load(self.releases)
        self.assertEqual((loaded["version"], loaded["flash_sha256"]), ("12.34.56", "a" * 64))

    def test_existing_verified_state_upgrades_marker_without_reseeding(self):
        expected = self.state.load(self.releases)
        self.state.marker.unlink()  # Earlier implementation had no marker.
        loaded = VerifiedState(self.state.root, "original").load(self.releases)
        self.assertEqual(loaded, expected)
        self.assertTrue(self.state.marker.is_file())

    def test_marker_creation_crash_blocks_restart(self):
        with patch("verified_state.os.fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                self.state.load(self.releases)
        self.assertFalse(self.state.healthy)
        self.assertFalse(self.state.root.exists())
        with self.assertRaises(ValueError):
            VerifiedState(self.state.root, "original").load(self.releases)