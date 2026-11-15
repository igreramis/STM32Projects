"""AWS IoT lab gateway; optional locally approved OTA, never arbitrary commands."""

import argparse
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import queue
import re
import signal
import threading
import time

from awscrt import mqtt
from awsiot import mqtt_connection_builder

from firmware import FirmwareStager, Release, StageError


LOG = logging.getLogger("pi-iot")
MAX_COMMAND_BYTES = 4096
MAX_COMMAND_AGE = 60
OPERATION_TIMEOUT = 20


@dataclass(frozen=True)
class Config:
    endpoint: str
    port: int
    client_id: str
    certificate: str
    private_key: str
    root_ca: str
    telemetry_interval_seconds: float

    @property
    def topic_prefix(self):
        return f"lab/{self.client_id}"

    @classmethod
    def load(cls, filename):
        data = json.loads(Path(filename).read_text())
        if not isinstance(data, dict):
            raise ValueError("Config must be a JSON object")
        if not isinstance(data.get("endpoint"), str) or not re.fullmatch(
            r"[a-zA-Z0-9.-]+", data["endpoint"]
        ):
            raise ValueError("Endpoint must be a hostname, without a URL scheme")
        if type(data.get("port")) is not int or data["port"] != 8883:
            raise ValueError("This lab uses MQTT mTLS port 8883")
        if data.get("client_id") != "pi-lab-01":
            raise ValueError("The installed IoT policy requires client ID pi-lab-01")
        interval = data.get("telemetry_interval_seconds")
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 1:
            raise ValueError("Telemetry interval must be finite and at least one second")
        for key in ("certificate", "private_key", "root_ca"):
            path = Path(data[key]).expanduser()
            if not path.is_file():
                raise ValueError(f"Missing {key} file")
            data[key] = str(path)
        return cls(**data)


def reject_nonfinite(value):
    raise ValueError("Non-finite JSON number")


def parse_command_object(payload):
    if len(payload) > MAX_COMMAND_BYTES:
        raise ValueError("Command exceeds size limit")
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=reject_nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON command") from exc
    if not isinstance(value, dict):
        raise ValueError("Command must be an object")
    command_id = value.get("command_id")
    if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", command_id):
        raise ValueError("Missing or invalid command_id")
    command = value.get("command")
    if not isinstance(command, str) or not 1 <= len(command) <= 64:
        raise ValueError("Missing or invalid command")
    return value


def parse_command(payload):
    value = parse_command_object(payload)
    return value["command_id"], value["command"]


def snapshot(client_id, telemetry_count):
    return {
        "device_id": client_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(float(Path("/proc/uptime").read_text().split()[0]), 2),
        "telemetry_count": telemetry_count,
    }


class CommandHandler:
    """Bounded replay cache; optional OTA installs use persistent deduplication."""

    def __init__(self, client_id, cache_limit=128, stager=None, ota=None):
        self.client_id = client_id
        self.cache_limit = cache_limit
        self.cache = OrderedDict()
        self.stager = stager
        self.ota = ota
        self.stage_status = {"status": "idle", "flashed": False}

    def handle(self, payload, telemetry_count, notify=None):
        value = parse_command_object(payload)
        command_id, command = value["command_id"], value["command"]
        # Destructive jobs use persistent dedup, never the evictable LRU.
        if command == "install_firmware" and self.ota is not None:
            return self.ota.submit(value)
        # Cache a digest, never a signed URL. Changed fields under an existing
        # ID are a conflict, including changed firmware version or URL.
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).digest()
        if command_id in self.cache:
            old_fingerprint, response = self.cache[command_id]
            self.cache.move_to_end(command_id)
            if old_fingerprint != fingerprint:
                return {"command_id": command_id, "status": "error", "error": "command_id_reused"}
            return response
        response = {"command_id": command_id, "device_id": self.client_id}
        if command == "get_status":
            response.update(status="ok", data=snapshot(self.client_id, telemetry_count))
            response["data"]["firmware_stage"] = self.stage_status.copy()
            if self.ota is not None:
                response["data"]["firmware_install"] = dict(self.ota.status())
        elif command == "stage_firmware" and self.stager is not None:
            try:
                self.stager.validate(value)
            except StageError as exc:
                response.update(status="error", error=str(exc), flashed=False)
            else:
                self.stage_status = dict(response, status="downloading", flashed=False)
                if notify is not None:
                    notify(self.stage_status.copy())
                try:
                    data = self.stager.stage(value)
                except StageError as exc:
                    response.update(status="error", error=str(exc), flashed=False)
                else:
                    response.update(status="verified", data=data, flashed=False)
                self.stage_status = response.copy()
        else:
            response.update(status="error", error="unsupported_command")
        self.cache[command_id] = (fingerprint, response)
        if len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)
        return response


class Gateway:
    def __init__(self, config, ota=None):
        self.config = config
        self.ota = ota
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.inbox = queue.Queue(maxsize=32)
        release = Release.load(Path(__file__).with_name("release.json"))
        self.handler = CommandHandler(
            config.client_id,
            stager=FirmwareStager(release, Path.home() / "aws-iot" / "staged"),
            ota=ota,
        )
        self.commands_topic = f"{config.topic_prefix}/commands"
        self.telemetry_count = 0
        self.connection = mqtt_connection_builder.mtls_from_path(
            endpoint=config.endpoint,
            port=config.port,
            cert_filepath=config.certificate,
            pri_key_filepath=config.private_key,
            ca_filepath=config.root_ca,
            client_id=config.client_id,
            clean_session=True,
            keep_alive_secs=30,
            reconnect_min_timeout_secs=1,
            reconnect_max_timeout_secs=30,
            protocol_operation_timeout_ms=10000,
            on_connection_interrupted=self.on_interrupted,
            on_connection_resumed=self.on_resumed,
        )

    def on_interrupted(self, connection, error, **kwargs):
        self.connected.clear()
        self.ready.clear()
        LOG.warning("Connection interrupted: %s; SDK will reconnect", error)

    def on_resumed(self, connection, return_code, session_present, **kwargs):
        if return_code != mqtt.ConnectReturnCode.ACCEPTED:
            self.failed.set()
            return
        self.connected.set()
        LOG.info("Connection resumed; session_present=%s", session_present)
        # Never wait for an SDK future inside its event-loop callback.
        if session_present:
            self.ready.set()
        else:
            try:
                self.subscribe().add_done_callback(self.on_subscribed)
            except Exception:
                LOG.exception("Could not request resubscription")
                self.failed.set()

    def on_subscribed(self, future):
        try:
            future.result()
        except Exception:
            LOG.exception("Command subscription failed")
            self.failed.set()
        else:
            if self.connected.is_set():
                self.ready.set()
            LOG.info("Subscribed to %s", self.commands_topic)

    def on_message(self, topic, payload, dup=False, qos=None, retain=False, **kwargs):
        if topic != self.commands_topic or retain or len(payload) > MAX_COMMAND_BYTES:
            LOG.warning("Ignored retained, oversized or unexpected-topic command")
            return
        try:
            self.inbox.put_nowait((time.monotonic(), payload))
        except queue.Full:
            LOG.warning("Command queue full; dropping message")

    def subscribe(self):
        future, _ = self.connection.subscribe(
            topic=self.commands_topic, qos=mqtt.QoS.AT_LEAST_ONCE, callback=self.on_message
        )
        return future

    def publish(self, suffix, data):
        future, _ = self.connection.publish(
            topic=f"{self.config.topic_prefix}/{suffix}",
            payload=json.dumps(data, allow_nan=False),
            qos=mqtt.QoS.AT_LEAST_ONCE,
            retain=False,
        )
        # At most one application publish is outstanding. A timeout terminates
        # this run rather than accumulating SDK offline publish operations.
        future.result(timeout=OPERATION_TIMEOUT)

    def run(self, duration=0):
        try:
            LOG.info("Connecting to %s:%s as %s", self.config.endpoint, self.config.port, self.config.client_id)
            self.connection.connect().result(timeout=OPERATION_TIMEOUT)
            self.connected.set()
            LOG.info("MQTT connection accepted")
            subscribed = self.subscribe()
            subscribed.result(timeout=OPERATION_TIMEOUT)
            self.on_subscribed(subscribed)
            if self.ota is not None:
                self.run_ota_loop(duration)
                return
            start = time.monotonic()
            next_telemetry = start
            while not self.stop.is_set():
                if duration and time.monotonic() - start >= duration:
                    break
                if self.failed.is_set():
                    raise RuntimeError("MQTT connection/subscription failed")
                if not self.connected.is_set() or not self.ready.is_set():
                    self.stop.wait(0.2)
                    continue
                if time.monotonic() >= next_telemetry:
                    next_count = self.telemetry_count + 1
                    self.publish("telemetry", snapshot(self.config.client_id, next_count))
                    self.telemetry_count = next_count
                    LOG.info("Telemetry PUBACK received; count=%d", next_count)
                    next_telemetry = time.monotonic() + self.config.telemetry_interval_seconds
                try:
                    received_at, payload = self.inbox.get(timeout=0.2)
                except queue.Empty:
                    continue
                if time.monotonic() - received_at > MAX_COMMAND_AGE:
                    LOG.warning("Dropped locally queued command older than %ds", MAX_COMMAND_AGE)
                    continue
                try:
                    response = self.handler.handle(
                        payload, self.telemetry_count,
                        notify=lambda progress: self.publish("acks", progress),
                    )
                except ValueError as exc:
                    LOG.warning("Rejected command: %s", exc)
                    continue
                self.publish("acks", response)
                LOG.info("Acknowledgement PUBACK received; command_id=%s status=%s",
                         response["command_id"], response["status"])
        finally:
            self.ready.clear()
            if self.ota is not None:
                # Preserve the lifetime job lock until the worker has stopped.
                self.ota.close()
            try:
                self.connection.disconnect().result(timeout=10)
            except Exception as exc:
                LOG.warning("Disconnect did not complete cleanly: %s", exc)

    def run_ota_loop(self, duration=0):
        """One outstanding MQTT publish; journal revisions survive disconnects.

        Never start another publish merely because a future is slow. On a
        completed failure the durable revision remains pending for retry.
        Installer runs in its own single worker, not in MQTT callbacks.
        """
        replies = queue.Queue(maxsize=64)
        pending = None
        start = time.monotonic()
        next_telemetry = start
        retry_at = start

        def enqueue(data):
            try:
                replies.put_nowait(data)
            except queue.Full:
                LOG.warning("Transient acknowledgement queue full")

        def needs_reply(data):
            if data.get("event_seq") is None:
                return True
            # Pending revisions already have a durable publishing path. Only
            # delivered final snapshots need a separate duplicate reply (and
            # transient retry); never queue accepted or progress snapshots.
            return (data.get("status") in ("application_verified", "error", "interrupted", "rejected")
                    and not any(item["command_id"] == data["command_id"]
                                for item in self.ota.pending_results()))

        while not self.stop.is_set():
            now = time.monotonic()
            if duration and now - start >= duration:
                break
            if self.failed.is_set():
                raise RuntimeError("MQTT connection/subscription failed")
            if pending is not None:
                future, suffix, data, submitted, warned = pending
                if future.done():
                    try:
                        future.result()
                    except Exception:
                        LOG.warning("Publish failed; durable OTA result retained")
                        retry_at = now + 5.0
                        if suffix == "acks" and needs_reply(data):
                            enqueue(data)
                    else:
                        if suffix == "acks" and data.get("event_seq") is not None:
                            self.ota.mark_delivered(data["command_id"], data["event_seq"])
                        elif suffix == "telemetry":
                            self.telemetry_count = data["telemetry_count"]
                        LOG.info("%s PUBACK received", suffix)
                    pending = None
                elif now - submitted >= OPERATION_TIMEOUT and not warned:
                    LOG.warning("PUBACK pending; retaining result, not duplicating publish")
                    pending = (future, suffix, data, submitted, True)

            online = self.connected.is_set() and self.ready.is_set()
            if online:
                try:
                    received_at, payload = self.inbox.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if now - received_at <= MAX_COMMAND_AGE:
                        try:
                            response = self.handler.handle(payload, self.telemetry_count, notify=enqueue)
                        except ValueError:
                            LOG.warning("Rejected invalid command")
                        else:
                            if needs_reply(response):
                                enqueue(response)
                    else:
                        LOG.warning("Dropped expired locally queued command")

            if online and pending is None and now >= retry_at:
                durable = self.ota.pending_results()
                data = None
                if durable:
                    suffix, data = "acks", durable[0]
                elif not replies.empty():
                    suffix, data = "acks", replies.get_nowait()
                elif now >= next_telemetry:
                    suffix, data = "telemetry", snapshot(self.config.client_id, self.telemetry_count + 1)
                    next_telemetry = now + self.config.telemetry_interval_seconds
                if data is not None:
                    try:
                        future, _ = self.connection.publish(
                            topic=f"{self.config.topic_prefix}/{suffix}",
                            payload=json.dumps(data, allow_nan=False),
                            qos=mqtt.QoS.AT_LEAST_ONCE, retain=False)
                    except Exception:
                        LOG.warning("Publish unavailable; durable OTA result retained")
                        retry_at = now + 5.0
                        if suffix == "acks" and needs_reply(data):
                            enqueue(data)
                    else:
                        pending = (future, suffix, data, now, False)
            self.stop.wait(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--duration", type=float, default=0, help="Seconds after subscription; 0 runs until stopped")
    parser.add_argument("--ota-config", help="Explicitly enable install_firmware using local approval/configuration")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("duration must be finite and nonnegative")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ota = None
    try:
        config = Config.load(args.config)
        if args.ota_config is not None:
            from ota_jobs import from_config
            ota = from_config(args.ota_config, config.client_id)
        gateway = Gateway(config, ota=ota)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: gateway.stop.set())
        gateway.run(args.duration)
    except Exception:
        LOG.exception("Client stopped with an error")
        return 1
    finally:
        if ota is not None:
            ota.close()
    LOG.info("Client stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())