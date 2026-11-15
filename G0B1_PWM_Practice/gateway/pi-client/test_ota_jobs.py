"""Coordinator-only, offline tests. Fake installers; no UART/cloud/deployment.

Events/barriers and bounded joins coordinate threads, never timing sleeps.
Interrupted journals are produced by suppressing only the worker's final commit,
then reopening the actual disk state. All files live in temporary directories.
"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import ota_jobs as ota


SECRET = "https://secret.invalid/firmware?X-Amz-Signature=TOP_SECRET"


def command(command_id="job-1", **changes):
    result = dict(command_id=command_id, command="install_firmware", version="0.2.0",
                  from_version="original", sha256="a" * 64, expires_at=1100)
    result.update(changes)
    return result


class GateInstaller:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def __call__(self, value, progress, stop):
        self.calls.append(dict(value))
        self.started.set()
        if not self.release.wait(5):
            raise AssertionError("test did not release installer")
        return {"status": "error", "error": "cancelled"}


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.now = 1000

    def jobs(self, installer=None):
        if installer is None:
            installer = Mock(return_value={"status": "error", "error": "cancelled"})
        jobs = ota.OtaJobs("pi-lab-01", self.root, installer, clock=lambda: self.now)
        self.addCleanup(jobs.close)
        if isinstance(installer, GateInstaller):
            self.addCleanup(installer.release.set)
        return jobs

    def join(self, jobs):
        jobs._worker.join(5)
        self.assertFalse(jobs._worker.is_alive(), "installer thread stuck")

    def record(self, command_id="job-1"):
        path = self.root / (hashlib.sha256(command_id.encode("ascii")).hexdigest() + ".json")
        return json.loads(path.read_text()), path

    def unfinished(self, stage=None, **flags):
        def installer(value, progress, stop):
            if stage:
                progress(stage, **flags)
            return {"status": "error", "error": "cancelled"}
        jobs = self.jobs(installer)
        with patch.object(jobs, "_finish"):
            jobs.submit(command())
            self.join(jobs)
        before, _ = self.record()
        jobs.close()
        replacement = self.jobs()
        return replacement, before

    def test_import_and_construction_do_not_start_worker(self):
        installer = Mock()
        jobs = self.jobs(installer)
        installer.assert_not_called()
        self.assertIsNone(jobs._worker)
        self.assertEqual(jobs.pending_results(), [])
        self.assertEqual(jobs.status()["status"], "idle")

    def test_acceptance_is_durable_before_launch_and_worker_non_daemon(self):
        observed = []
        def installer(value, progress, stop):
            observed.append(self.record()[0])
            observed.append(threading.current_thread().daemon)
            return {"status": "error", "error": "cancelled"}
        jobs = self.jobs(installer)
        response = jobs.submit(command())
        self.join(jobs)
        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["event_seq"], 1)
        self.assertEqual(observed[0]["status"], "accepted")
        self.assertIs(observed[0]["flashed"], False)
        self.assertFalse(observed[1])

    def test_atomic_mode_and_fsync_order(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        real_fsync, real_replace = os.fsync, os.replace
        actions = []
        def fsync(fd):
            actions.append("directory" if fd == jobs._root_fd else "file")
            real_fsync(fd)
        def replace(source, target):
            actions.append("replace")
            real_replace(source, target)
        with patch.object(ota.os, "fsync", side_effect=fsync), patch.object(
                ota.os, "replace", side_effect=replace):
            jobs.submit(command())
        self.assertEqual(actions, ["file", "replace", "directory"])
        _, path = self.record()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(".ota-*")), [])

    def test_duplicates_and_changed_fingerprint_never_relaunch(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        first = command(url=SECRET)
        accepted = jobs.submit(first)
        self.assertTrue(gate.started.wait(3))
        self.assertEqual(jobs.submit(dict(reversed(list(first.items())))), accepted)
        self.assertEqual(jobs.submit(command(url=SECRET + "x"))["error"], "command_id_reused")
        self.assertEqual(jobs.submit(command(version="0.3.0"))["error"], "command_id_reused")
        self.assertEqual(len(gate.calls), 1)
        fingerprint = self.record()[0]["fingerprint"]
        expected = hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=True).encode("ascii")).hexdigest()
        self.assertEqual(fingerprint, expected)

    def test_completed_disk_restart_dedup_and_acked_tombstone(self):
        jobs = self.jobs()
        jobs.submit(command())
        self.join(jobs)
        latest = jobs.pending_results()[0]
        self.assertTrue(jobs.mark_delivered(latest["command_id"], latest["event_seq"]))
        jobs.close()
        installer = Mock()
        replacement = self.jobs(installer)
        self.now = 9999  # Duplicate lookup precedes expiry rejection.
        self.assertEqual(replacement.submit(command()), latest)
        self.assertEqual(replacement.pending_results(), [])
        self.assertTrue(self.record()[1].exists())
        installer.assert_not_called()

    def test_restart_unfinished_before_erase_is_interrupted_not_rerun(self):
        jobs, old = self.unfinished("waiting_for_rom")
        result = jobs.pending_results()[0]
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["phase"], "waiting_for_rom")
        self.assertIs(result["flashed"], False)
        self.assertEqual(result["event_seq"], old["delivery_seq"] + 1)
        self.assertEqual(jobs.submit(command()), result)
        jobs.installer.assert_not_called()

    def test_restart_accepted_is_interrupted(self):
        jobs, _ = self.unfinished()
        self.assertEqual(jobs.status()["status"], "interrupted")
        self.assertIs(jobs.status()["flashed"], False)

    def test_restart_erase_intent_is_unknown_flash(self):
        jobs, _ = self.unfinished("erase_started", flash_may_be_modified=True)
        self.assertIsNone(jobs.status()["flashed"])
        self.assertTrue(jobs.status()["flash_may_be_modified"])
        self.assertFalse(jobs.status()["flash_verified"])
        self.assertFalse(jobs.status()["application_health_verified"])

    def test_restart_waiting_for_app_retains_verified_flash(self):
        jobs, _ = self.unfinished("waiting_for_application", flash_verified=True, flashed=True)
        result = jobs.status()
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["flashed"])
        self.assertTrue(result["flash_verified"])
        self.assertFalse(result["application_health_verified"])

    def test_repeated_restart_does_not_create_new_interruption_revision(self):
        jobs, _ = self.unfinished("erase_started")
        first = jobs.pending_results()
        jobs.close()
        replacement = self.jobs()
        self.assertEqual(replacement.pending_results(), first)

    def test_strict_schema_rejects_without_storage_or_worker(self):
        installer = Mock()
        jobs = self.jobs(installer)
        invalid = [None, [], "secret", {}, command(command_id="../bad"),
                   command(command_id="x" * 65), command(command_id=True),
                   command(command="stage_firmware"), command(version="0.4.0"),
                   command(from_version="latest"), command(sha256="a" * 63),
                   command(sha256="g" * 64), command(expires_at=True),
                   command(expires_at=1100.0), command(expires_at="1100"),
                   command(url=True), command(url="a" * 3501),
                   command(authenticated=True), command(authorized=True),
                   command(extra=SECRET)]
        for key in command():
            missing = command()
            del missing[key]
            invalid.append(missing)
        for value in invalid:
            with self.subTest(value=value):
                result = jobs.submit(value)
                self.assertEqual(result["error"], "invalid_arguments")
                self.assertIsNone(result["event_seq"])
                self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(list(self.root.glob("*.json")), [])
        installer.assert_not_called()

    def test_expiry_bounds_and_nonfinite_clock(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        for expires in (0, 999, 1000, 2801):
            self.assertEqual(jobs.submit(command(expires_at=expires))["error"], "command_expired")
        for now in (float("nan"), float("inf")):
            self.now = now
            self.assertEqual(jobs.submit(command())["error"], "command_expired")
        self.now = 1000
        self.assertEqual(jobs.submit(command(expires_at=2800))["status"], "accepted")

    def test_uppercase_sha_is_fingerprinted_original_but_installer_gets_lowercase(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        value = command(sha256="A" * 64, url="x" * 3500)
        self.assertEqual(jobs.submit(value)["status"], "accepted")
        self.assertTrue(gate.started.wait(3))
        self.assertEqual(gate.calls[0]["sha256"], "a" * 64)
        self.assertEqual(jobs.submit(command(sha256="a" * 64, url="x" * 3500))["error"],
                         "command_id_reused")

    def test_busy_rejection_is_durable_and_never_autoexecutes(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        jobs.submit(command())
        self.assertTrue(gate.started.wait(3))
        rejected = jobs.submit(command("job-2"))
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["error"], "installer_busy")
        self.assertEqual(rejected["event_seq"], 1)
        gate.release.set()
        self.join(jobs)
        self.assertEqual(jobs.submit(command("job-2")), rejected)
        jobs.close()
        replacement = self.jobs()
        self.assertEqual(replacement.submit(command("job-2")), rejected)
        replacement.installer.assert_not_called()

    def test_capacity_fails_closed_without_eviction_even_after_ack(self):
        self.assertEqual(ota.MAX_RECORDS, 1024)
        gate = GateInstaller()
        jobs = self.jobs(gate)
        with patch.object(ota, "MAX_RECORDS", 2):
            accepted = jobs.submit(command())
            rejected = jobs.submit(command("job-2"))
            jobs.mark_delivered("job-2", rejected["event_seq"])
            self.assertEqual(jobs.submit(command("job-3"))["error"], "state_capacity_exceeded")
            self.assertEqual(jobs.submit(command()), accepted)
            self.assertEqual(len(list(self.root.glob("*.json"))), 2)

    def test_directory_flock_rejects_same_root_and_alias(self):
        jobs = self.jobs()
        with self.assertRaisesRegex(RuntimeError, "^ota_state_locked$"):
            self.jobs()
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "^ota_state_locked$"):
            ota.OtaJobs("pi-lab-01", alias, Mock())
        jobs.close()
        self.jobs()  # Released only after close.

    def test_flock_also_excludes_another_process(self):
        self.jobs()
        script = ("import sys; from ota_jobs import OtaJobs\n"
                  "try: OtaJobs('pi-lab-01', sys.argv[1], lambda *a: {})\n"
                  "except RuntimeError as e:\n"
                  " sys.exit(0 if str(e) == 'ota_state_locked' else 2)\n"
                  "sys.exit(3)\n")
        result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                cwd=str(Path(ota.__file__).parent), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_failed_acceptance_never_launches_and_refuses_new_work(self):
        installer = Mock()
        jobs = self.jobs(installer)
        with patch.object(ota.os, "replace", side_effect=OSError(SECRET)):
            result = jobs.submit(command(url=SECRET))
        self.assertEqual(result["error"], "storage_error")
        self.assertIsNone(result["event_seq"])
        self.assertFalse(jobs.status()["healthy"])
        self.assertEqual(jobs.submit(command("job-2"))["error"], "storage_error")
        self.assertEqual(list(self.root.glob("*.json")), [])
        self.assertEqual(list(self.root.glob(".ota-*")), [])
        installer.assert_not_called()

    def test_directory_fsync_failure_after_replace_is_never_retried(self):
        installer = Mock()
        jobs = self.jobs(installer)
        real_fsync = os.fsync
        def fail_directory(fd):
            if fd == jobs._root_fd:
                raise OSError(SECRET)
            real_fsync(fd)
        with patch.object(ota.os, "fsync", side_effect=fail_directory):
            self.assertEqual(jobs.submit(command())["error"], "storage_error")
        self.assertEqual(self.record()[0]["status"], "accepted")
        installer.assert_not_called()
        jobs.close()
        replacement = self.jobs(installer)
        self.assertEqual(replacement.submit(command())["status"], "interrupted")
        installer.assert_not_called()

    def test_file_fsync_failure_does_not_replace(self):
        jobs = self.jobs()
        with patch.object(ota.os, "fsync", side_effect=OSError(SECRET)), patch.object(ota.os, "replace") as replace:
            self.assertEqual(jobs.submit(command())["error"], "storage_error")
        replace.assert_not_called()
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_progress_persistence_failure_prevents_destructive_action_even_if_caught(self):
        destructive = threading.Event()
        def installer(value, progress, stop):
            try:
                with patch.object(jobs, "_atomic_write", side_effect=OSError(SECRET)):
                    progress("erase_started", flash_may_be_modified=True)
                destructive.set()
            except Exception:
                return {"status": "error", "error": "programming_failed", "flashed": False}
        jobs = self.jobs(installer)
        jobs.submit(command())
        self.join(jobs)
        self.assertFalse(destructive.is_set())
        self.assertFalse(jobs.status()["healthy"])
        self.assertIsNone(jobs.status()["flashed"])
        self.assertTrue(jobs.status()["flash_may_be_modified"])
        self.assertTrue(jobs.stop_event.is_set())
        self.assertEqual(self.record()[0]["status"], "accepted")
        with self.assertRaisesRegex(ota.StorageError, "^storage_error$"):
            jobs.pending_results()

    def test_post_erase_exception_preserves_unknown_flash_and_sanitizes(self):
        def installer(value, progress, stop):
            progress("erase_started", flash_may_be_modified=True)
            progress("programming", flash_may_be_modified=False, flashed=False)
            raise RuntimeError(SECRET)
        jobs = self.jobs(installer)
        jobs.submit(command())
        self.join(jobs)
        result = jobs.pending_results()[0]
        self.assertEqual(result["error"], "internal_error")
        self.assertIsNone(result["flashed"])
        self.assertTrue(result["flash_may_be_modified"])

    def test_verified_flags_sticky_across_progress_and_error_result(self):
        def installer(value, progress, stop):
            progress("flash_verified", flash_verified=True, flashed=True)
            progress("waiting_for_application", flash_verified=False, flashed=False,
                     flash_may_be_modified=False)
            return {"status": "error", "error": "application_start_timeout",
                    "flash_verified": False, "flashed": False}
        jobs = self.jobs(installer)
        jobs.submit(command())
        self.join(jobs)
        result = jobs.pending_results()[0]
        self.assertTrue(result["flashed"])
        self.assertTrue(result["flash_verified"])
        self.assertTrue(result["flash_may_be_modified"])
        self.assertFalse(result["application_health_verified"])
        self.assertEqual(result["error"], "application_start_timeout")

    def test_success_requires_application_evidence(self):
        jobs = self.jobs(lambda *args: {"status": "application_verified", "flashed": True})
        jobs.submit(command())
        self.join(jobs)
        self.assertEqual(jobs.pending_results()[0]["error"], "internal_error")
        self.assertIs(jobs.pending_results()[0]["flashed"], False)

    def test_success_is_durable_with_all_safety_flags(self):
        jobs = self.jobs(lambda *args: {"status": "application_verified", "flashed": True,
                                      "flash_verified": True, "application_health_verified": True})
        jobs.submit(command())
        self.join(jobs)
        result = jobs.pending_results()[0]
        self.assertEqual(result["status"], "application_verified")
        for field in ("flashed", "flash_verified", "flash_may_be_modified", "application_health_verified"):
            self.assertTrue(result[field])
        self.assertNotIn("error", result)

    def test_puback_old_revision_cannot_ack_newer_progress_or_completion(self):
        progressed, advance, finish = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(advance.set)
        self.addCleanup(finish.set)
        def installer(value, progress, stop):
            self.assertTrue(advance.wait(5))
            progress("downloading")
            progressed.set()
            self.assertTrue(finish.wait(5))
            return {"status": "error", "error": "download_failed"}
        jobs = self.jobs(installer)
        self.addCleanup(finish.set)
        self.addCleanup(advance.set)
        accepted = jobs.submit(command())
        self.assertTrue(jobs.mark_delivered("job-1", accepted["event_seq"]))
        self.assertEqual(jobs.pending_results(), [])
        advance.set()
        self.assertTrue(progressed.wait(3))
        downloading = jobs.pending_results()[0]
        self.assertEqual(downloading["event_seq"], 2)
        self.assertFalse(jobs.mark_delivered("job-1", 1))
        self.assertFalse(jobs.mark_delivered("job-1", 99))
        self.assertFalse(jobs.mark_delivered("job-1", True))
        self.assertFalse(jobs.mark_delivered("unknown", 2))
        finish.set()
        self.join(jobs)
        self.assertFalse(jobs.mark_delivered("job-1", downloading["event_seq"]))
        latest = jobs.pending_results()[0]
        self.assertEqual(latest["event_seq"], 3)
        self.assertTrue(jobs.mark_delivered("job-1", latest["event_seq"]))
        self.assertTrue(jobs.mark_delivered("job-1", latest["event_seq"]))
        self.assertEqual(jobs.pending_results(), [])
        self.assertTrue(self.record()[1].exists())

    def test_ack_write_failure_fails_closed_without_resetting_flags(self):
        jobs = self.jobs(lambda *args: {"status": "error", "error": "programming_failed",
                                      "flash_may_be_modified": True})
        jobs.submit(command())
        self.join(jobs)
        latest = jobs.pending_results()[0]
        with patch.object(jobs, "_atomic_write", side_effect=OSError(SECRET)):
            with self.assertRaises(ota.StorageError):
                jobs.mark_delivered("job-1", latest["event_seq"])
        self.assertFalse(jobs.status()["healthy"])
        self.assertIsNone(jobs.status()["flashed"])
        jobs.close()
        replacement = self.jobs()
        self.assertEqual(replacement.pending_results(), [latest])

    def test_snapshots_are_immutable_json_dicts_and_detached(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        accepted = jobs.submit(command())
        self.assertIsInstance(accepted, dict)
        self.assertEqual(json.loads(json.dumps(accepted))["status"], "accepted")
        with self.assertRaises(TypeError):
            accepted["status"] = "tampered"
        with self.assertRaises(TypeError):
            accepted.update(status="tampered")
        with self.assertRaises(TypeError):
            accepted.pop("status")
        pending = jobs.pending_results()
        pending.clear()
        self.assertEqual(len(jobs.pending_results()), 1)
        gate.release.set()
        self.join(jobs)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["event_seq"], 1)

    def test_caller_command_mutation_does_not_change_worker_command(self):
        entered, inspect = threading.Event(), threading.Event()
        captured = []
        def installer(value, progress, stop):
            entered.set()
            self.assertTrue(inspect.wait(5))
            captured.append(dict(value))
            return {"status": "error", "error": "cancelled"}
        jobs = self.jobs(installer)
        self.addCleanup(inspect.set)
        value = command(url=SECRET)
        jobs.submit(value)
        self.assertTrue(entered.wait(3))
        value.update(version="0.3.0", url="changed")
        inspect.set()
        self.join(jobs)
        self.assertEqual(captured[0]["version"], "0.2.0")
        self.assertEqual(captured[0]["url"], SECRET)

    def test_concurrent_duplicate_submissions_launch_only_once(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        barrier = threading.Barrier(3)
        results = []
        def submit():
            barrier.wait(timeout=3)
            results.append(jobs.submit(command()))
        callers = [threading.Thread(target=submit) for _ in range(2)]
        for thread in callers:
            thread.start()
        barrier.wait(timeout=3)
        for thread in callers:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertTrue(gate.started.wait(3))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(gate.calls), 1)

    def test_close_sets_worker_stop_joins_and_keeps_flock_until_worker_exits(self):
        started, cancelled, release = threading.Event(), threading.Event(), threading.Event()
        captured = []
        def installer(value, progress, stop):
            captured.append(stop)
            started.set()
            self.assertTrue(stop.wait(5))
            cancelled.set()
            self.assertTrue(release.wait(5))
            return {"status": "error", "error": "cancelled"}
        jobs = self.jobs(installer)
        self.addCleanup(release.set)
        jobs.submit(command())
        self.assertTrue(started.wait(3))
        closer = threading.Thread(target=jobs.close)
        closer.start()
        self.assertTrue(cancelled.wait(3))
        self.assertIs(captured[0], jobs.stop_event)
        self.assertTrue(closer.is_alive())
        self.assertTrue(jobs.status()["active"])
        with self.assertRaisesRegex(RuntimeError, "ota_state_locked"):
            self.jobs()
        self.assertEqual(jobs.submit(command("job-2"))["error"], "coordinator_closed")
        release.set()
        closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertFalse(jobs.status()["active"])
        self.assertEqual(self.record()[0]["error"], "cancelled")
        jobs.close()  # Idempotent.
        self.jobs()

    def test_stop_before_progress_prevents_destructive_action(self):
        destructive = threading.Event()
        def installer(value, progress, stop):
            stop.set()
            progress("erase_started")
            destructive.set()
        jobs = self.jobs(installer)
        jobs.submit(command())
        self.join(jobs)
        self.assertFalse(destructive.is_set())
        self.assertIs(jobs.pending_results()[0]["flashed"], False)

    def test_no_urls_in_journal_snapshots_or_exception_output(self):
        retained = []
        def installer(value, progress, stop):
            retained.append(value)
            self.assertEqual(value["url"], SECRET)
            progress("downloading", url=SECRET, error=SECRET, run_directory=SECRET,
                     device_id=SECRET, version=SECRET, arbitrary={"url": SECRET})
            raise RuntimeError(SECRET)
        jobs = self.jobs(installer)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            accepted = jobs.submit(command(url=SECRET))
            self.join(jobs)
        blob = json.dumps([accepted, jobs.status(), jobs.pending_results()]) + output.getvalue()
        blob += "".join(path.read_text() for path in self.root.iterdir() if path.is_file())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("TOP_SECRET", blob)
        self.assertNotIn('"url"', blob)
        self.assertEqual(retained, [{}])
        self.assertEqual(jobs.pending_results()[0]["error"], "internal_error")

    def test_unknown_progress_and_result_errors_are_sanitized(self):
        for installer in (lambda value, progress, stop: progress(SECRET),
                          lambda *args: {"status": "error", "error": SECRET},
                          lambda *args: None):
            jobs = self.jobs(installer)
            jobs.submit(command(str(len(list(self.root.glob("*.json"))))))
            self.join(jobs)
            self.assertEqual(jobs.status()["error"], "internal_error")
            self.assertNotIn(SECRET, json.dumps(jobs.status()))
            jobs.close()

    def test_thread_start_failure_leaves_terminal_tombstone(self):
        jobs = self.jobs()
        with patch.object(ota.threading.Thread, "start", side_effect=RuntimeError(SECRET)):
            result = jobs.submit(command())
        self.assertEqual(result["error"], "internal_error")
        self.assertEqual(result["status"], "error")
        self.assertEqual(jobs.submit(command()), result)
        jobs.installer.assert_not_called()

    def test_launch_and_final_commit_failure_does_not_return_undurable_revision(self):
        jobs = self.jobs()
        write = jobs._atomic_write
        def fail_final(record):
            if record["status"] == "error":
                raise OSError(SECRET)
            write(record)
        with patch.object(jobs, "_atomic_write", side_effect=fail_final), patch.object(
                ota.threading.Thread, "start", side_effect=RuntimeError(SECRET)):
            result = jobs.submit(command())
        self.assertEqual(result["error"], "storage_error")
        self.assertIsNone(result["event_seq"])
        self.assertFalse(jobs.status()["healthy"])
        self.assertEqual(self.record()[0]["status"], "accepted")
        jobs.installer.assert_not_called()

    def test_erase_directory_fsync_failure_retains_unknown_flash_on_restart(self):
        destructive = threading.Event()
        def installer(value, progress, stop):
            real_fsync = os.fsync
            def fail_directory(fd):
                if fd == jobs._root_fd:
                    raise OSError(SECRET)
                real_fsync(fd)
            with patch.object(ota.os, "fsync", side_effect=fail_directory):
                progress("erase_started", flash_may_be_modified=True)
            destructive.set()
        jobs = self.jobs(installer)
        jobs.submit(command())
        self.join(jobs)
        self.assertFalse(destructive.is_set())
        self.assertIsNone(jobs.status()["flashed"])
        self.assertFalse(jobs.status()["healthy"])
        self.assertIsNone(self.record()[0]["flashed"])
        jobs.close()
        replacement = self.jobs()
        self.assertEqual(replacement.status()["status"], "interrupted")
        self.assertIsNone(replacement.status()["flashed"])
        replacement.installer.assert_not_called()

    def test_final_commit_failure_keeps_verified_evidence_and_never_writes_false_flags(self):
        def installer(value, progress, stop):
            progress("flash_verified", flash_verified=True)
            return {"status": "error", "error": "application_start_timeout", "flashed": False}
        jobs = self.jobs(installer)
        write = jobs._atomic_write
        def fail_final(record):
            if record["status"] == "error":
                raise OSError(SECRET)
            write(record)
        with patch.object(jobs, "_atomic_write", side_effect=fail_final):
            jobs.submit(command())
            self.join(jobs)
        self.assertFalse(jobs.status()["healthy"])
        self.assertTrue(jobs.status()["flashed"])
        self.assertTrue(self.record()[0]["flash_verified"])
        jobs.close()
        replacement = self.jobs()
        self.assertTrue(replacement.status()["flashed"])
        self.assertEqual(replacement.status()["status"], "interrupted")

    def test_late_phase_without_verification_evidence_is_unknown_not_safe(self):
        jobs, _ = self.unfinished("waiting_for_application")
        self.assertIsNone(jobs.status()["flashed"])
        self.assertTrue(jobs.status()["flash_may_be_modified"])
        self.assertFalse(jobs.status()["flash_verified"])

    def test_corrupt_journal_fails_closed_and_releases_lock(self):
        jobs = self.jobs()
        jobs.submit(command())
        self.join(jobs)
        original, path = self.record()
        jobs.close()
        path.write_text('{"url": "' + SECRET + '"}')
        with self.assertRaisesRegex(ota.StorageError, "^storage_error$"):
            self.jobs()
        path.write_text(json.dumps(original))
        self.jobs()  # Failed construction did not retain the flock.

    def test_mismatched_client_or_filename_fails_closed(self):
        jobs = self.jobs()
        jobs.submit(command())
        self.join(jobs)
        _, path = self.record()
        jobs.close()
        with self.assertRaises(ota.StorageError):
            ota.OtaJobs("another-device", self.root, Mock())
        path.rename(self.root / "wrong.json")
        with self.assertRaises(ota.StorageError):
            self.jobs()

    def test_over_capacity_on_restart_fails_closed(self):
        gate = GateInstaller()
        jobs = self.jobs(gate)
        jobs.submit(command())
        jobs.submit(command("job-2"))
        gate.release.set()
        self.join(jobs)
        jobs.close()
        with patch.object(ota, "MAX_RECORDS", 1), self.assertRaises(ota.StorageError):
            self.jobs()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "ota.json"
        self.config = dict(catalog="missing/catalog.json", release_root="missing/releases",
                           baseline="missing/baseline.bin", run_root="runs", state_root="state",
                           port="/dev/serial0", download_releases={"0.2.0": "metadata/release.json"})

    def test_disabled_config_has_no_imports_or_io(self):
        with patch("builtins.__import__", side_effect=AssertionError("unexpected import")), patch.object(
                ota.Path, "read_text", side_effect=AssertionError("unexpected IO")):
            self.assertIsNone(ota.from_config(None, "pi-lab-01"))

    def test_lazy_factory_resolves_paths_without_requiring_resources(self):
        self.path.write_text(json.dumps(self.config))
        fake = Mock()
        with patch.dict(sys.modules, {"ota_installer": fake}):
            jobs = ota.from_config(self.path, "pi-lab-01")
        self.addCleanup(jobs.close)
        kwargs = fake.Installer.call_args.kwargs
        self.assertEqual(kwargs["catalog"], str(self.root / "missing/catalog.json"))
        self.assertEqual(kwargs["release_root"], str(self.root / "missing/releases"))
        self.assertEqual(kwargs["baseline"], str(self.root / "missing/baseline.bin"))
        self.assertEqual(kwargs["run_root"], str(self.root / "runs"))
        self.assertEqual(kwargs["port"], str(Path("/dev/serial0").resolve()))
        self.assertEqual(kwargs["download_releases"], {"0.2.0": str(self.root / "metadata/release.json")})
        self.assertEqual(jobs.state_root, self.root / "state")
        self.assertFalse((self.root / "missing").exists())
        fake.Installer.return_value.assert_not_called()
        self.assertIsNone(jobs._worker)

    def test_port_symlink_resolves_to_device_path(self):
        alias = self.root / "serial-alias"
        target = self.root / "device"
        alias.symlink_to(target)
        self.config["port"] = str(alias)
        self.path.write_text(json.dumps(self.config))
        fake = Mock()
        with patch.dict(sys.modules, {"ota_installer": fake}):
            jobs = ota.from_config(self.path, "pi-lab-01")
        self.addCleanup(jobs.close)
        self.assertEqual(fake.Installer.call_args.kwargs["port"], str(target))

    def test_install_disabled_rejects_before_worker_or_journal_write(self):
        self.config["install_enabled"] = False
        self.path.write_text(json.dumps(self.config))
        fake = Mock()
        with patch.dict(sys.modules, {"ota_installer": fake}):
            jobs = ota.from_config(self.path, "pi-lab-01")
        self.addCleanup(jobs.close)
        self.assertFalse(jobs.install_enabled)
        response = jobs.submit(dict(command_id="disabled-test", command="install_firmware",
            version="0.3.0", from_version="0.2.0", sha256="a" * 64,
            expires_at=int(jobs.clock()) + 60))
        self.assertEqual(response["error"], "deployment_disabled")
        self.assertIsNone(response["event_seq"])
        self.assertIsNone(jobs._worker)
        self.assertEqual(list(jobs.state_root.glob("*.json")), [])
        fake.Installer.return_value.assert_not_called()

    def test_download_mapping_optional(self):
        self.config.pop("download_releases")
        self.path.write_text(json.dumps(self.config))
        fake = Mock()
        with patch.dict(sys.modules, {"ota_installer": fake}):
            jobs = ota.OtaJobs.from_config(self.path, "pi-lab-01")
        self.addCleanup(jobs.close)
        self.assertEqual(fake.Installer.call_args.kwargs["download_releases"], {})

    def test_strict_config_sanitizes_unknown_missing_invalid_and_duplicate_keys(self):
        cases = [[], {}, dict(self.config, arbitrary=SECRET), dict(self.config, port=2),
                 dict(self.config, install_enabled="false"), dict(self.config, install_enabled=0),
                 dict(self.config, baseline=""), dict(self.config, run_root=None),
                 dict(self.config, catalog="\x00"), dict(self.config, download_releases=[]),
                 dict(self.config, download_releases={"0.4.0": "metadata"}),
                 dict(self.config, download_releases={"0.2.0": False})]
        for key in ("catalog", "release_root", "baseline", "run_root", "state_root", "port"):
            missing = dict(self.config)
            del missing[key]
            cases.append(missing)
        for value in cases:
            self.path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "^invalid_ota_config$"):
                ota.from_config(self.path, "pi-lab-01")
        self.path.write_text('{"catalog":"one", "catalog":"two"}')
        with self.assertRaisesRegex(ValueError, "^invalid_ota_config$"):
            ota.from_config(self.path, "pi-lab-01")
        self.assertFalse((self.root / "state").exists())


if __name__ == "__main__":
    unittest.main()