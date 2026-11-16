"""Offline request/configuration tests, including optional real-SDK presigning."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import aws_adapter
from backend import Conflict, canonical

try:
    import boto3
    from botocore.config import Config
except ImportError:
    boto3 = None


class ServiceError(Exception):
    def __init__(self, response):
        super().__init__("offline-service-error")
        self.response = response


class DynamoStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec_set=["get_item", "query", "transact_write_items"])
        self.store = aws_adapter.DynamoStore(self.client, "offline-table")

    def item(self, row, revision=7):
        return {"data": {"S": canonical(row).decode()}, "revision": {"N": str(revision)}}

    def test_get_consistent_read_decode_preserves_json_types(self):
        row = {"count": 2**53, "fraction": 0.25, "flag": True, "empty": None, "nested": [1, False]}
        self.client.get_item.return_value = {"Item": self.item(row)}
        self.assertEqual(self.store.get("job", "one"), dict(row, _revision=7))
        self.client.get_item.assert_called_once_with(TableName="offline-table",
            Key={"pk": {"S": "job"}, "sk": {"S": "one"}}, ConsistentRead=True)
        self.client.get_item.return_value = {}
        self.assertIsNone(self.store.get("job", "missing"))
        self.assertIsNone(self.store.decode({}))

    def test_list_paginates_even_across_empty_page_with_consistent_reads(self):
        cursor = {"pk": {"S": "job"}, "sk": {"S": "one"}}
        cursor2 = {"pk": {"S": "job"}, "sk": {"S": "two"}}
        self.client.query.side_effect = [
            {"Items": [self.item({"value": 1})], "LastEvaluatedKey": cursor},
            {"Items": [], "LastEvaluatedKey": cursor2},
            {"Items": [self.item({"value": 2}, 9)]}]
        self.assertEqual(self.store.list("job"), [{"value": 1, "_revision": 7}, {"value": 2, "_revision": 9}])
        calls = self.client.query.call_args_list
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertTrue(call.kwargs["ConsistentRead"])
            self.assertEqual(call.kwargs["KeyConditionExpression"], "pk = :kind")
            self.assertEqual(call.kwargs["ExpressionAttributeValues"], {":kind": {"S": "job"}})
        self.assertNotIn("ExclusiveStartKey", calls[0].kwargs)
        self.assertEqual(calls[1].kwargs["ExclusiveStartKey"], cursor)
        self.assertEqual(calls[2].kwargs["ExclusiveStartKey"], cursor2)

    def test_transaction_create_update_delete_and_read_condition(self):
        old = {"value": 1, "_revision": 7}
        after = {"value": 2, "_revision": 999}
        original = deepcopy((old, after))
        self.store.commit([("job", "new", None, {"value": True}),
                           ("job", "existing", old, after),
                           ("lease", "gateway", old, None),
                           ("release", "version", old, old)])
        self.client.transact_write_items.assert_called_once()
        operations = self.client.transact_write_items.call_args.kwargs["TransactItems"]
        create, update, delete, check = (operations[0]["Put"], operations[1]["Put"],
                                         operations[2]["Delete"], operations[3]["ConditionCheck"])
        self.assertEqual(create, {"TableName": "offline-table", "ConditionExpression": "attribute_not_exists(pk)",
            "Item": {"pk": {"S": "job"}, "sk": {"S": "new"}, "revision": {"N": "1"},
                     "data": {"S": '{"value":true}'}}})
        self.assertEqual(update["Item"]["revision"], {"N": "8"})
        self.assertEqual(json.loads(update["Item"]["data"]["S"]), {"value": 2})
        for operation in (update, delete, check):
            self.assertEqual(operation["ConditionExpression"], "revision = :revision")
            self.assertEqual(operation["ExpressionAttributeValues"], {":revision": {"N": "7"}})
            self.assertEqual(operation["TableName"], "offline-table")
        self.assertEqual(delete["Key"], self.store.key("lease", "gateway"))
        self.assertEqual(check["Key"], self.store.key("release", "version"))
        self.assertEqual((old, after), original)

    def test_equal_copy_is_an_update_not_a_condition_check(self):
        row = {"value": 1, "_revision": 2}
        self.store.commit([("job", "one", row, dict(row))])
        operation = self.client.transact_write_items.call_args.kwargs["TransactItems"][0]
        self.assertIn("Put", operation)
        self.assertEqual(operation["Put"]["Item"]["revision"], {"N": "3"})

    def test_absent_job_and_lease_are_condition_checks_not_deletes(self):
        self.store.commit([("job", "request", None, None), ("lease", "gateway", None, None)])
        operations = self.client.transact_write_items.call_args.kwargs["TransactItems"]
        for operation, kind, identifier in zip(operations, ("job", "lease"), ("request", "gateway")):
            self.assertEqual(operation, {"ConditionCheck": {"TableName": "offline-table",
                "ConditionExpression": "attribute_not_exists(pk)", "Key": self.store.key(kind, identifier)}})

    def test_conditional_cancellation_maps_to_conflict_without_retry(self):
        self.client.transact_write_items.side_effect = ServiceError({
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}]})
        with self.assertRaises(Conflict):
            self.store.commit([("job", "one", None, {"value": 1})])
        self.client.transact_write_items.assert_called_once()

    def test_service_and_nonconditional_cancellations_propagate_without_retry(self):
        errors = [RuntimeError("offline-error"), ServiceError({"Error": {"Code": "AccessDeniedException"}}),
            ServiceError({"Error": {"Code": "TransactionCanceledException"}}),
            ServiceError({"Error": {"Code": "TransactionCanceledException"},
                          "CancellationReasons": [{"Code": "TransactionConflict"}]}),
            ServiceError({"Error": {"Code": "TransactionCanceledException"},
                          "CancellationReasons": [{"Code": "ProvisionedThroughputExceeded"}]})]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.client.reset_mock()
                self.client.transact_write_items.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self.store.commit([("job", "one", None, {"value": 1})])
                self.assertIs(caught.exception, error)
                self.client.transact_write_items.assert_called_once()


class ApplicationTests(unittest.TestCase):
    def test_factory_uses_injected_sdk_modules_and_disables_publish_retries(self):
        boto3 = ModuleType("boto3")
        clients = {name: Mock(name=name) for name in ("dynamodb", "s3", "kms", "iot-data")}
        boto3.client = Mock(side_effect=lambda name, **kwargs: clients[name])
        config_module = ModuleType("botocore.config")

        class Config:
            def __init__(self, **kwargs):
                self.options = kwargs

        config_module.Config = Config
        env = {"AWS_REGION": "ap-southeast-2", "TABLE_NAME": "offline-table",
               "IOT_DATA_ENDPOINT": "iot.offline.invalid", "FIRMWARE_BUCKET": "offline-bucket",
               "SIGNING_KEY_ARN": "offline-signing-key", "COGNITO_ISSUER": "offline-issuer",
               "COGNITO_CLIENT_ID": "offline-client"}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {
                "boto3": boto3, "botocore": ModuleType("botocore"), "botocore.config": config_module}), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            app = aws_adapter.application()
        self.assertIs(app.store.client, clients["dynamodb"])
        self.assertEqual(app.store.table, "offline-table")
        self.assertIs(app.s3, clients["s3"])
        self.assertIs(app.kms, clients["kms"])
        self.assertIs(app.iot, clients["iot-data"])
        self.assertEqual((app.bucket, app.region, app.signing_key, app.issuer, app.client_id),
                         (env["FIRMWARE_BUCKET"], env["AWS_REGION"], env["SIGNING_KEY_ARN"],
                          env["COGNITO_ISSUER"], env["COGNITO_CLIENT_ID"]))
        calls = {call.args[0]: call.kwargs for call in boto3.client.call_args_list}
        self.assertEqual(boto3.client.call_count, 4)
        self.assertEqual(calls["s3"]["region_name"], "ap-southeast-2")
        self.assertEqual(calls["s3"]["endpoint_url"], "https://s3.ap-southeast-2.amazonaws.com")
        self.assertEqual(calls["s3"]["config"].options,
                 {"signature_version": "s3v4", "s3": {"addressing_style": "virtual"}})
        self.assertEqual(calls["dynamodb"], {})
        self.assertEqual(calls["kms"], {})
        self.assertEqual(calls["iot-data"]["endpoint_url"], "https://iot.offline.invalid")
        self.assertEqual(calls["iot-data"]["config"].options, {
            "retries": {"total_max_attempts": 1, "mode": "standard"},
            "connect_timeout": 3, "read_timeout": 5})
        for client in clients.values():
            self.assertEqual(client.mock_calls, [])


@unittest.skipIf(boto3 is None, "boto3 is not installed; SDK URL regression NOT checked")
class S3PresigningTests(unittest.TestCase):
    def setUp(self):
        env = {"AWS_REGION": "ap-southeast-2", "AWS_DEFAULT_REGION": "ap-southeast-2",
               "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
               "BOTO_CONFIG": os.devnull, "AWS_EC2_METADATA_DISABLED": "true",
               "TABLE_NAME": "offline-table", "FIRMWARE_BUCKET": "iot-lab-firmware-844274146091",
               "IOT_DATA_ENDPOINT": "iot.offline.invalid", "SIGNING_KEY_ARN": "offline-key",
               "COGNITO_ISSUER": "offline-issuer", "COGNITO_CLIENT_ID": "offline-client"}
        for guard in (patch.dict(os.environ, env, clear=True),
                      patch("socket.socket", side_effect=AssertionError("network forbidden")),
                      patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
                      patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden")),
                      patch("botocore.client.BaseClient._make_api_call",
                            side_effect=AssertionError("AWS API calls forbidden"))):
            guard.start()
            self.addCleanup(guard.stop)
        self.session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                                     region_name=env["AWS_REGION"])
        default_session = patch.object(boto3, "DEFAULT_SESSION", self.session)
        default_session.start()
        self.addCleanup(default_session.stop)
        gateway = Path(__file__).resolve().parents[1] / "gateway" / "pi-client"
        with patch.object(sys, "path", [str(gateway), *sys.path]):
            import firmware
        self.firmware = firmware
        self.release = firmware.Release(target="NUCLEO-G0B1RE", version="0.4.0", size_bytes=8,
            sha256="0" * 64, bucket=env["FIRMWARE_BUCKET"], region=env["AWS_REGION"],
            key="firmware/stm32g0b1/0.4.0/firmware.bin", version_id="qfhEPtXZJ7m5eUvmreMgBanFwSGJqYQG")
        # validate() only needs release; avoid constructor CA-file/opener I/O.
        self.stager = firmware.FirmwareStager.__new__(firmware.FirmwareStager)
        self.stager.release = self.release

    def presign(self, s3):
        return s3.generate_presigned_url("get_object", Params={"Bucket": self.release.bucket,
            "Key": self.release.key, "VersionId": self.release.version_id}, ExpiresIn=900)

    def command(self, url):
        return dict(command_id="00000000-0000-4000-8000-000000000040", command="stage_firmware",
                    version=self.release.version, url=url)

    def test_actual_application_presigns_versioned_url_accepted_by_gateway(self):
        app = aws_adapter.application()
        url = self.presign(app.s3)
        self.assertEqual(urlsplit(url).hostname, self.release.host)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["versionId"], [self.release.version_id])
        self.assertEqual(query["X-Amz-Algorithm"], ["AWS4-HMAC-SHA256"])
        self.assertEqual(query["X-Amz-Expires"], ["900"])
        self.assertIn("/ap-southeast-2/s3/aws4_request", query["X-Amz-Credential"][0])
        self.assertTrue(self.stager.validate(self.command(url)) == url)

    @unittest.skipUnless(boto3 is not None and boto3.__version__ == "1.37.38",
                         "Original endpoint behavior reproducer targets boto3 1.37.38")
    def test_original_signature_only_config_produces_gateway_rejected_global_host(self):
        # Proves the old configuration's behavior, not an unretained historical URL.
        url = self.presign(self.session.client("s3", config=Config(signature_version="s3v4")))
        self.assertEqual(urlsplit(url).hostname, self.release.bucket + ".s3.amazonaws.com")
        with self.assertRaisesRegex(self.firmware.StageError, "^url_not_approved$"):
            self.stager.validate(self.command(url))


if __name__ == "__main__":
    unittest.main()