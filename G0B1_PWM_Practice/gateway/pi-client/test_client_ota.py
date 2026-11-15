"""Offline client/OTA integration tests using the real durable coordinator.

MQTT futures, gateway events, time and worker scheduling are controlled here:
no sleeps, network, hardware, or background threads. Journal I/O is confined to
TemporaryDirectory. Coordinator internals (including old-revision ACK rejection)
are covered separately in test_ota_jobs; these tests exercise client routing and
the actual run_ota_loop across those public coordinator boundaries.
"""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from awscrt import mqtt
import client
import ota_jobs


def install(command_id="install-1", **changes):
    value = dict(command_id=command_id, command="install_firmware",
                 version="0.2.0", from_version="original", sha256="a" * 64,
                 expires_at=1100)
    value.update(changes)
    return value


def payload(value):
    return json.dumps(value).encode("utf-8")


class FakeFuture:
    """An unresolved result() is a test failure, never a blocking wait."""

    def __init__(self, complete=False):
        self.complete = complete
        self.error = None
        self.result_calls = []

    def done(self):
        return self.complete

    def result(self, timeout=None):
        self.result_calls.append(timeout)
        if not self.complete:
            raise AssertionError("client waited on an unresolved future")
        if self.error is not None:
            raise self.error
        return {}

    def ack(self):
        self.complete = True

    def fail(self):
        self.error = RuntimeError("simulated MQTT publish failure")
        self.complete = True


class FakeEvent:
    def __init__(self, value=False):
        self.value = value
        self.on_wait = None

    def set(self):
        self.value = True

    def clear(self):
        self.value = False

    def is_set(self):
        return self.value

    def wait(self, timeout=None):
        if self.on_wait is None:
            raise AssertionError("unexpected event wait")
        self.on_wait(timeout)
        return self.value


class ControlledWorker:
    """start schedules work; finish executes it synchronously exactly once."""

    def __init__(self, target, args, name, daemon):
        self.target, self.args = target, args
        self.ident = None
        self.alive = False
        self.executed = False

    def start(self):
        self.ident = 1
        self.alive = True

    def is_alive(self):
        return self.alive

    def finish(self):
        if self.executed:
            raise AssertionError("worker rerun")
        self.executed = True
        try:
            self.target(*self.args)
        finally:
            self.alive = False

    def join(self):
        if self.alive:
            self.finish()


class ClientOtaTests(unittest.TestCase):
    def patched(self, target, attribute, **kwargs):
        patcher = patch.object(target, attribute, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = 0.0
        self.patched(client, "time", new=SimpleNamespace(
            monotonic=lambda: self.now,
            sleep=Mock(side_effect=AssertionError("real sleep forbidden"))))
        self.patched(client, "snapshot", side_effect=lambda device, count: {
            "device_id": device, "telemetry_count": count, "uptime_seconds": self.now})
        self.worker_factory = self.patched(
            ota_jobs.threading, "Thread", side_effect=ControlledWorker)
        self.installer = Mock(return_value={"status": "error", "error": "cancelled"})
        self.jobs = ota_jobs.OtaJobs("pi-lab-01", Path(temporary.name) / "state",
                                    self.installer, clock=lambda: 1000)
        self.addCleanup(self.jobs.close)
        self.submit = self.patched(self.jobs, "submit", wraps=self.jobs.submit)
        self.delivered = self.patched(self.jobs, "mark_delivered", wraps=self.jobs.mark_delivered)
        self.connection = Mock()
        self.connection.connect.return_value = FakeFuture(complete=True)
        self.connection.subscribe.return_value = (FakeFuture(complete=True), 1)
        self.connection.disconnect.return_value = FakeFuture(complete=True)
        self.connection.publish.side_effect = self.publish
        self.patched(client.mqtt_connection_builder, "mtls_from_path", return_value=self.connection)
        self.patched(client.Release, "load", return_value=object())
        self.stager = Mock()
        self.patched(client, "FirmwareStager", return_value=self.stager)
        self.config = client.Config("example.invalid", 8883, "pi-lab-01",
                                    "unused-cert", "unused-key", "unused-ca", 10)
        self.gateway = client.Gateway(self.config, ota=self.jobs)
        self.gateway.connected = FakeEvent(True)
        self.gateway.ready = FakeEvent(True)
        self.gateway.failed = FakeEvent()
        self.gateway.stop = FakeEvent()
        self.publications = []
        self.outcomes = []

    def publish(self, **kwargs):
        self.assertTrue(all(isinstance(item["future"], Exception) or item["future"].done()
                            for item in self.publications), "overlapping MQTT publishes")
        future = self.outcomes.pop(0) if self.outcomes else FakeFuture()
        self.publications.append(dict(kwargs, data=json.loads(kwargs["payload"]),
                                      future=future, at=self.now))
        if isinstance(future, Exception):
            raise future
        return future, len(self.publications)

    def send(self, value):
        self.gateway.on_message(self.gateway.commands_topic, payload(value))

    def at(self, value):
        self.now = value

    def ack(self):
        self.publications[-1]["future"].ack()

    def drive(self, *steps, duration=0, through_run=False):
        """Run one loop turn, then one more per callback, without wall time."""
        remaining = iter(steps)
        self.gateway.stop.clear()

        def advance(timeout):
            self.assertEqual(timeout, 0.05)
            self.now += timeout
            try:
                step = next(remaining)
            except StopIteration:
                self.gateway.stop.set()
            else:
                step()

        self.gateway.stop.on_wait = advance
        if through_run:
            self.gateway.run(duration)
        else:
            self.gateway.run_ota_loop(duration)

    def acknowledgements(self):
        return [item["data"] for item in self.publications
                if item["topic"].endswith("/acks")]

    def assert_pending(self, sequence=1):
        self.assertEqual([item["event_seq"] for item in self.jobs.pending_results()], [sequence])
        self.delivered.assert_not_called()

    def test_default_gateway_and_handler_leave_install_unsupported(self):
        gateway = client.Gateway(self.config)
        self.assertIsNone(gateway.ota)
        self.assertIsNone(gateway.handler.ota)
        response = gateway.handler.handle(payload(install()), 0)
        self.assertEqual(response["error"], "unsupported_command")
        self.assertIsNone(response.get("event_seq"))
        self.submit.assert_not_called()
        self.worker_factory.assert_not_called()
        self.stager.stage.assert_not_called()

    def test_install_routes_to_durable_submit_not_lru_even_after_eviction(self):
        handler = self.gateway.handler
        handler.cache_limit = 1
        # A non-install response with this ID must not intercept an OTA request.
        handler.handle(payload(dict(command_id="install-1", command="get_status")), 0)
        cached = handler.cache.copy()
        accepted = handler.handle(payload(install()), 0)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(handler.cache, cached)
        handler.handle(payload(dict(command_id="other", command="get_status")), 0)
        self.assertNotIn("install-1", handler.cache)
        self.assertEqual(handler.handle(payload(install()), 0), accepted)
        conflict = handler.handle(payload(install(version="0.3.0")), 0)
        self.assertEqual(conflict["error"], "command_id_reused")
        self.assertEqual(self.submit.call_args_list,
                         [call(install()), call(install()), call(install(version="0.3.0"))])
        self.worker_factory.assert_called_once()
        self.assertEqual(list(handler.cache), ["other"])

    def test_get_status_includes_detached_install_snapshot_only_when_enabled(self):
        self.jobs.submit(install())
        response = self.gateway.handler.handle(
            payload(dict(command_id="status", command="get_status")), 7)
        self.assertEqual(response["data"]["telemetry_count"], 7)
        self.assertEqual(response["data"]["firmware_install"], self.jobs.status())
        self.assertTrue(response["data"]["firmware_install"]["active"])
        response["data"]["firmware_install"]["status"] = "changed copy"
        self.assertEqual(self.jobs.status()["status"], "accepted")
        disabled = client.CommandHandler("pi-lab-01").handle(
            payload(dict(command_id="status", command="get_status")), 0)
        self.assertNotIn("firmware_install", disabled["data"])

    def test_stage_success_progress_and_cached_replay_unchanged_with_ota(self):
        value = dict(command_id="stage", command="stage_firmware", version="0.2.0",
                     url="https://example.invalid/approved-image")
        self.stager.stage.return_value = {"sha256": "a" * 64, "flashed": False}
        notify = Mock()
        result = self.gateway.handler.handle(payload(value), 0, notify=notify)
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["flashed"], False)
        self.assertEqual(result["data"], self.stager.stage.return_value)
        notify.assert_called_once_with(dict(command_id="stage", device_id="pi-lab-01",
                                            status="downloading", flashed=False))
        self.assertEqual(self.gateway.handler.stage_status, result)
        self.assertEqual(self.gateway.handler.handle(payload(value), 1, notify=notify), result)
        self.stager.validate.assert_called_once_with(value)
        self.stager.stage.assert_called_once_with(value)
        self.submit.assert_not_called()

    def test_stage_validation_and_download_errors_stay_transient(self):
        for method in ("validate", "stage"):
            with self.subTest(method=method):
                self.stager.validate.side_effect = None
                self.stager.stage.side_effect = None
                getattr(self.stager, method).side_effect = client.StageError("test_stage_error")
                notify = Mock()
                result = self.gateway.handler.handle(payload(dict(
                    command_id="stage-" + method, command="stage_firmware")), 0, notify=notify)
                self.assertEqual(result["error"], "test_stage_error")
                self.assertIs(result["flashed"], False)
                self.assertNotIn("event_seq", result)
                self.assertEqual(notify.call_count, 0 if method == "validate" else 1)
        self.submit.assert_not_called()

    def test_stage_loop_publishes_progress_then_verified_without_durable_ack(self):
        self.stager.stage.return_value = {"sha256": "a" * 64, "flashed": False}
        self.send(dict(command_id="stage", command="stage_firmware", version="0.2.0",
                       url="https://example.invalid/approved-image"))
        self.drive(self.ack, self.ack)
        replies = self.acknowledgements()
        self.assertEqual([item["status"] for item in replies], ["downloading", "verified"])
        self.assertTrue(all(item["flashed"] is False for item in replies))
        self.assertTrue(all(item.get("event_seq") is None for item in replies))
        self.delivered.assert_not_called()
        self.submit.assert_not_called()
        self.stager.stage.assert_called_once()

    def test_install_accepted_published_once_not_also_as_transient_reply(self):
        self.send(install())
        self.drive(self.ack, self.ack, lambda: None)
        self.assertEqual(len(self.acknowledgements()), 1)
        self.assertEqual(self.acknowledgements()[0]["status"], "accepted")
        self.delivered.assert_called_once_with("install-1", 1)
        self.assertEqual(self.jobs.pending_results(), [])
        self.assertEqual(self.gateway.telemetry_count, 1)

    def test_duplicate_install_after_final_puback_replays_without_rerunning_worker(self):
        self.send(install())
        terminal = []

        def finish_and_ack_accepted():
            self.jobs._worker.finish()
            terminal.append(self.jobs.pending_results()[0])
            self.ack()

        def duplicate_after_final_puback():
            self.assertEqual(self.jobs.pending_results(), [])
            self.assertEqual(self.delivered.call_args_list,
                             [call("install-1", 1), call("install-1", 2)])
            self.assertTrue(self.publications[-1]["topic"].endswith("/telemetry"))
            self.send(install())

        def ack_telemetry_before_replay():
            self.assertEqual(len(self.publications), 3)
            self.assertEqual(self.submit.call_count, 2)
            self.ack()

        def ack_replay():
            self.assertEqual(self.acknowledgements()[1:], [terminal[0], terminal[0]])
            self.assertEqual(self.delivered.call_count, 2)
            self.assertEqual(self.publications[-1]["future"].result_calls, [])
            self.ack()

        self.drive(finish_and_ack_accepted, self.ack, duplicate_after_final_puback,
                   ack_telemetry_before_replay, ack_replay)
        self.assertEqual(self.delivered.call_args_list,
                         [call("install-1", 1), call("install-1", 2), call("install-1", 2)])
        self.assertEqual(self.jobs.pending_results(), [])
        self.assertEqual(self.jobs.status()["event_seq"], 2)
        self.worker_factory.assert_called_once()
        self.installer.assert_called_once()

    def test_duplicate_accepted_and_pending_final_do_not_queue_extra_replies(self):
        self.send(install())

        def duplicate():
            self.send(install())

        def finish():
            self.jobs._worker.finish()

        self.drive(duplicate, self.ack, duplicate, self.ack, finish,
                   duplicate, self.ack, lambda: None)
        self.assertEqual([item["event_seq"] for item in self.acknowledgements()], [1, 2])
        self.assertEqual(self.submit.call_count, 4)
        self.assertEqual(self.delivered.call_args_list,
                         [call("install-1", 1), call("install-1", 2)])
        self.assertEqual(self.jobs.pending_results(), [])
        self.worker_factory.assert_called_once()
        self.installer.assert_called_once()

    def check_delivered_final_replay_retry(self, synchronous):
        self.jobs.submit(install())
        self.jobs._worker.finish()
        terminal = self.jobs.pending_results()[0]
        self.drive(self.ack, self.ack)
        self.assertEqual(self.jobs.pending_results(), [])
        self.delivered.reset_mock()
        self.send(install())
        failure = RuntimeError("publish unavailable") if synchronous else FakeFuture()
        self.outcomes.append(failure)
        previous = len(self.publications)

        def fail():
            if not synchronous:
                failure.fail()

        def before_retry():
            self.assertEqual(len(self.publications), previous + 1)
            self.assertEqual(self.jobs.pending_results(), [])
            self.delivered.assert_not_called()
            self.at(10)

        def ack_retry():
            self.assertEqual(len(self.publications), previous + 2)
            self.assertEqual(self.acknowledgements(), [terminal, terminal, terminal])
            self.delivered.assert_not_called()
            self.ack()

        with self.assertLogs("pi-iot", level="WARNING"):
            self.drive(fail, before_retry, ack_retry)
        self.delivered.assert_called_once_with("install-1", terminal["event_seq"])
        self.assertEqual(self.jobs.pending_results(), [])
        self.worker_factory.assert_called_once()
        self.installer.assert_called_once()

    def test_delivered_final_replay_retries_failed_future(self):
        self.check_delivered_final_replay_retry(synchronous=False)

    def test_delivered_final_replay_retries_synchronous_publish_failure(self):
        self.check_delivered_final_replay_retry(synchronous=True)

    def test_pending_result_qos1_and_mark_delivered_only_after_puback(self):
        accepted = self.jobs.submit(install())

        def before_ack():
            self.assert_pending()
            item = self.publications[0]
            self.assertEqual(item["data"], accepted)
            self.assertEqual(item["topic"], "lab/pi-lab-01/acks")
            self.assertEqual(item["qos"], mqtt.QoS.AT_LEAST_ONCE)
            self.assertIs(item["retain"], False)
            self.assertEqual(item["future"].result_calls, [])
            item["future"].ack()

        self.drive(lambda: self.assert_pending(), before_ack)
        self.delivered.assert_called_once_with("install-1", 1)
        self.assertEqual(self.publications[0]["future"].result_calls, [None])
        self.assertEqual(self.jobs.pending_results(), [])

    def test_terminal_error_publish_failure_retries_after_five_seconds_without_worker_rerun(self):
        self.jobs.submit(install())
        self.jobs._worker.finish()
        terminal = self.jobs.pending_results()[0]
        self.assertEqual(terminal["status"], "error")

        def fail_at_one():
            self.at(1)
            self.publications[0]["future"].fail()

        def before_retry():
            self.assertEqual(len(self.publications), 1)
            self.assertEqual(self.jobs.pending_results(), [terminal])
            self.delivered.assert_not_called()
            self.at(6)

        with self.assertLogs("pi-iot", level="WARNING"):
            self.drive(fail_at_one, lambda: self.at(5.999), before_retry, self.ack)
        self.assertEqual([item["at"] for item in self.publications[:2]], [0, 6])
        self.assertEqual(self.acknowledgements(), [terminal, terminal])
        self.delivered.assert_called_once_with("install-1", terminal["event_seq"])
        self.assertEqual(self.jobs.pending_results(), [])
        self.installer.assert_called_once()
        self.worker_factory.assert_called_once()
        self.submit.assert_called_once()

    def test_synchronous_publish_error_retains_result_and_obeys_cooldown(self):
        accepted = self.jobs.submit(install())
        self.outcomes.append(RuntimeError("publish unavailable"))

        def retry_boundary():
            self.assertEqual(len(self.publications), 1)
            self.assert_pending()
            self.at(5)

        with self.assertLogs("pi-iot", level="WARNING"):
            self.drive(lambda: self.at(4.999), retry_boundary, self.ack)
        self.assertEqual(self.acknowledgements(), [accepted, accepted])
        self.assertEqual(self.publications[1]["at"], 5)
        self.delivered.assert_called_once_with("install-1", 1)
        self.worker_factory.assert_called_once()

    def test_slow_future_never_duplicates_even_after_twenty_seconds(self):
        self.jobs.submit(install())

        def still_pending():
            self.assertEqual(len(self.publications), 1)
            self.assert_pending()
            self.assertEqual(self.publications[0]["future"].result_calls, [])

        with self.assertLogs("pi-iot", level="WARNING") as logs:
            self.drive(lambda: self.at(19.999), lambda: self.at(20),
                       lambda: self.at(200), still_pending)
        self.assertEqual(sum("PUBACK pending" in line for line in logs.output), 1)
        still_pending()

    def test_disconnect_reconnect_keeps_same_unresolved_publish(self):
        self.jobs.submit(install())

        def interrupt():
            self.gateway.on_interrupted(self.connection, "offline")
            self.at(30)

        def reconnect():
            self.assertEqual(len(self.publications), 1)
            self.assert_pending()
            self.gateway.on_resumed(self.connection, mqtt.ConnectReturnCode.ACCEPTED, True)

        with self.assertLogs("pi-iot", level="WARNING"):
            self.drive(interrupt, reconnect, self.ack)
        self.assertEqual(len(self.acknowledgements()), 1)
        self.delivered.assert_called_once_with("install-1", 1)
        self.worker_factory.assert_called_once()

    def test_failed_publish_offline_waits_for_reconnect_before_retry(self):
        accepted = self.jobs.submit(install())

        def fail_offline():
            self.gateway.on_interrupted(self.connection, "offline")
            self.publications[0]["future"].fail()
            self.at(1)

        def reconnect():
            self.assertEqual(len(self.publications), 1)
            self.assert_pending()
            self.gateway.on_resumed(self.connection, mqtt.ConnectReturnCode.ACCEPTED, True)

        with self.assertLogs("pi-iot", level="WARNING"):
            self.drive(fail_offline, lambda: self.at(100), reconnect, self.ack)
        self.assertEqual(self.acknowledgements(), [accepted, accepted])
        self.delivered.assert_called_once_with("install-1", 1)
        self.worker_factory.assert_called_once()

    def test_no_publish_until_both_connected_and_subscription_ready(self):
        self.jobs.submit(install())
        self.gateway.connected.clear()
        self.gateway.ready.clear()

        def connected_only():
            self.assertEqual(self.publications, [])
            self.gateway.connected.set()

        def subscribed():
            self.assertEqual(self.publications, [])
            self.gateway.on_subscribed(FakeFuture(complete=True))

        self.drive(connected_only, subscribed, self.ack)
        self.delivered.assert_called_once_with("install-1", 1)

    def test_newest_sequence_published_after_old_future_ack(self):
        accepted = self.jobs.submit(install())

        def finish_while_old_publish_pending():
            self.jobs._worker.finish()
            self.assertEqual(self.jobs.pending_results()[0]["event_seq"], 2)
            self.assertEqual(self.publications[0]["data"], accepted)
            self.ack()

        def ack_newest():
            self.assertEqual([item["event_seq"] for item in self.acknowledgements()], [1, 2])
            self.assertEqual(self.jobs.pending_results()[0]["event_seq"], 2)
            self.delivered.assert_called_once_with("install-1", 1)
            self.ack()

        self.drive(finish_while_old_publish_pending, ack_newest)
        self.assertEqual(self.delivered.call_args_list, [call("install-1", 1), call("install-1", 2)])
        self.assertEqual(self.jobs.pending_results(), [])
        self.installer.assert_called_once()

    def test_retry_reads_newest_revision_instead_of_failed_snapshot(self):
        self.jobs.submit(install())

        def fail():
            self.publications[0]["future"].fail()
            self.at(1)

        def finish_during_cooldown():
            self.jobs._worker.finish()
            self.at(6)

        with self.assertLogs("pi-iot", level="WARNING"):
            self.drive(fail, finish_during_cooldown, self.ack)
        self.assertEqual([item["event_seq"] for item in self.acknowledgements()], [1, 2])
        self.delivered.assert_called_once_with("install-1", 2)
        self.installer.assert_called_once()

    def test_status_and_periodic_telemetry_run_while_installer_is_active(self):
        observed = []

        def installer(value, progress, stop):
            # The actual coordinator worker calls this fake installer. Reenter
            # the gateway here to model its concurrent work deterministically.
            progress("waiting_for_rom")
            self.send(dict(command_id="status", command="get_status"))
            self.drive(self.ack, self.ack, self.ack, lambda: self.at(11), self.ack)
            observed.append(self.jobs.status()["active"])
            return {"status": "error", "error": "cancelled"}

        self.installer.side_effect = installer
        self.jobs.submit(install())
        self.jobs._worker.finish()
        self.assertEqual(observed, [True], "installer did not finish gateway checks")
        status = next(item for item in self.acknowledgements() if item["command_id"] == "status")
        self.assertTrue(status["data"]["firmware_install"]["active"])
        self.assertEqual(status["data"]["firmware_install"]["phase"], "waiting_for_rom")
        telemetry = [item["data"] for item in self.publications
                     if item["topic"].endswith("/telemetry")]
        self.assertEqual([item["telemetry_count"] for item in telemetry], [1, 2])
        self.assertEqual(self.gateway.telemetry_count, 2)
        self.installer.assert_called_once()

    def test_telemetry_count_advances_only_on_successful_puback(self):
        def unacked():
            self.assertEqual(self.gateway.telemetry_count, 0)
            self.assertEqual(self.publications[0]["future"].result_calls, [])
            self.ack()

        self.drive(lambda: None, unacked)
        self.assertEqual(self.gateway.telemetry_count, 1)
        self.assertEqual(self.publications[0]["qos"], mqtt.QoS.AT_LEAST_ONCE)
        self.delivered.assert_not_called()

    def test_transient_rejection_failure_retries_without_journal_ack(self):
        self.send(install(expires_at=999))

        def fail():
            self.publications[0]["future"].fail()
            self.at(1)

        with self.assertLogs("pi-iot", level="WARNING"):
            self.drive(fail, lambda: self.at(6), self.ack)
        replies = self.acknowledgements()
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0]["error"], "command_expired")
        self.assertIsNone(replies[0]["event_seq"])
        self.delivered.assert_not_called()
        self.worker_factory.assert_not_called()

    def test_run_finally_closes_ota_before_disconnect_on_normal_stop(self):
        lifecycle = Mock()
        close = self.patched(self.jobs, "close", wraps=self.jobs.close)
        lifecycle.attach_mock(close, "close")
        lifecycle.attach_mock(self.connection.disconnect, "disconnect")
        self.jobs.submit(install())
        self.drive(through_run=True)
        self.assertEqual(lifecycle.mock_calls, [call.close(), call.disconnect()])
        self.assertFalse(self.gateway.ready.is_set())
        self.assertTrue(self.jobs.status()["closed"])
        self.assertFalse(self.jobs.status()["active"])
        self.assertEqual(self.jobs.pending_results()[0]["error"], "cancelled")
        self.delivered.assert_not_called()

    def test_run_finally_closes_ota_on_connect_subscribe_or_loop_failure(self):
        for failure in ("connect", "subscribe", "loop"):
            with self.subTest(failure=failure):
                connection = Mock()
                connection.connect.return_value = FakeFuture(complete=True)
                subscribed = FakeFuture(complete=True)
                connection.subscribe.return_value = (subscribed, 1)
                connection.disconnect.return_value = FakeFuture(complete=True)
                jobs = Mock()
                gateway = self.gateway
                with patch.object(gateway, "connection", connection), patch.object(
                        gateway, "ota", jobs), patch.object(gateway, "run_ota_loop") as loop:
                    if failure == "loop":
                        loop.side_effect = RuntimeError("loop failed")
                    else:
                        future = connection.connect.return_value if failure == "connect" else subscribed
                        future.fail()
                    with self.assertRaises(RuntimeError):
                        gateway.run()
                jobs.close.assert_called_once_with()
                connection.disconnect.assert_called_once_with()
                self.assertFalse(gateway.ready.is_set())
                self.assertEqual(loop.call_count, 1 if failure == "loop" else 0)

    def test_duration_exit_closes_real_coordinator_with_unacked_result(self):
        self.jobs.submit(install())
        self.drive(lambda: self.at(1), duration=1, through_run=True)
        self.assertTrue(self.jobs.status()["closed"])
        self.connection.disconnect.assert_called_once()
        self.delivered.assert_not_called()

    def test_failed_flag_exits_actual_ota_loop_via_run_finally(self):
        self.gateway.failed.set()
        with self.assertRaisesRegex(RuntimeError, "MQTT connection/subscription failed"):
            self.drive(through_run=True)
        self.assertTrue(self.jobs.status()["closed"])
        self.connection.disconnect.assert_called_once()
        self.assertFalse(self.gateway.ready.is_set())
        self.assertEqual(self.publications, [])

    def test_main_defaults_to_disabled_without_constructing_coordinator(self):
        with patch.object(sys, "argv", ["client.py"]), patch.object(
                client.Config, "load", return_value=self.config), patch.object(
                client, "Gateway") as gateway, patch.object(
                ota_jobs, "from_config") as factory, patch.object(
                client.signal, "signal"), patch.object(client.logging, "basicConfig"):
            self.assertEqual(client.main(), 0)
        factory.assert_not_called()
        gateway.assert_called_once_with(self.config, ota=None)
        gateway.return_value.run.assert_called_once_with(0)

    def test_main_explicit_ota_configuration_is_passed_to_gateway(self):
        with patch.object(sys, "argv", ["client.py", "--ota-config", "local-approval.json"]), patch.object(
                client.Config, "load", return_value=self.config), patch.object(
                client, "Gateway") as gateway, patch.object(
                ota_jobs, "from_config", return_value=self.jobs) as factory, patch.object(
                client.signal, "signal"), patch.object(client.logging, "basicConfig"):
            self.assertEqual(client.main(), 0)
        factory.assert_called_once_with("local-approval.json", "pi-lab-01")
        gateway.assert_called_once_with(self.config, ota=self.jobs)
        self.assertTrue(self.jobs.status()["closed"])


if __name__ == "__main__":
    unittest.main()