import json
import queue
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import Mock

from awscrt import mqtt
from client import CommandHandler, Gateway, MAX_COMMAND_BYTES, parse_command


def command(command_id="test-1", name="get_status"):
    return json.dumps({"command_id": command_id, "command": name}).encode()


class CommandTests(unittest.TestCase):
    def test_valid_command(self):
        self.assertEqual(parse_command(command()), ("test-1", "get_status"))

    def test_invalid_commands(self):
        for payload in (b"not json", b"[]", b"{}", b"\xff", b"null",
                        b"[" * 2000, b"x" * (MAX_COMMAND_BYTES + 1),
                        b'{"command_id": "x", "command": NaN}',
                        command("bad\nid"), command("x" * 65), command(name="")):
            with self.subTest(payload=payload[:50]):
                with self.assertRaises(ValueError):
                    parse_command(payload)

    def test_status_and_duplicate(self):
        handler = CommandHandler("pi-lab-01")
        first = handler.handle(command(), 3)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["data"]["telemetry_count"], 3)
        self.assertGreaterEqual(first["data"]["uptime_seconds"], 0)
        self.assertEqual(handler.handle(command(), 9), first)

    def test_unsupported_command(self):
        self.assertEqual(CommandHandler("pi-lab-01").handle(command(name="reboot"), 0)["error"],
                         "unsupported_command")

    def test_conflicting_id(self):
        handler = CommandHandler("pi-lab-01")
        handler.handle(command(), 0)
        self.assertEqual(handler.handle(command(name="reboot"), 0)["error"], "command_id_reused")

    def test_bounded_cache(self):
        handler = CommandHandler("pi-lab-01", cache_limit=2)
        for i in range(3):
            handler.handle(command(str(i)), i)
        self.assertEqual(list(handler.cache), ["1", "2"])


class CallbackTests(unittest.TestCase):
    def setUp(self):
        # Exercise SDK callbacks without creating any network connection.
        self.gateway = Gateway.__new__(Gateway)
        self.gateway.connected = threading.Event()
        self.gateway.ready = threading.Event()
        self.gateway.failed = threading.Event()
        self.gateway.inbox = queue.Queue(maxsize=2)
        self.gateway.commands_topic = "lab/pi-lab-01/commands"

    def test_retained_and_oversized_ignored(self):
        g = self.gateway
        g.on_message(g.commands_topic, command(), retain=True)
        g.on_message(g.commands_topic, b"x" * (MAX_COMMAND_BYTES + 1))
        g.on_message("other", command())
        self.assertTrue(g.inbox.empty())

    def test_queue_bounded(self):
        g = self.gateway
        for _ in range(3):
            g.on_message(g.commands_topic, command())
        self.assertEqual(g.inbox.qsize(), 2)
        self.assertLessEqual(g.inbox.get()[0], time.monotonic())

    def test_interruption_clears_ready(self):
        g = self.gateway
        g.connected.set()
        g.ready.set()
        g.on_interrupted(None, "test")
        self.assertFalse(g.connected.is_set())
        self.assertFalse(g.ready.is_set())

    def test_resume_resubscribes_asynchronously(self):
        g = self.gateway
        future = Future()
        g.subscribe = Mock(return_value=future)
        g.on_resumed(None, mqtt.ConnectReturnCode.ACCEPTED, False)
        self.assertFalse(g.ready.is_set())
        future.set_result({"qos": mqtt.QoS.AT_LEAST_ONCE})
        self.assertTrue(g.ready.is_set())
        g.subscribe.assert_called_once()

    def test_subscription_failure(self):
        future = Future()
        future.set_exception(RuntimeError("test rejection"))
        with self.assertLogs("pi-iot", level="ERROR"):
            self.gateway.on_subscribed(future)
        self.assertTrue(self.gateway.failed.is_set())


if __name__ == "__main__":
    unittest.main()