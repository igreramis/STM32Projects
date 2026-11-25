"""Validate AWS request shapes and conditional writes without any network."""
import os
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

import aws_adapter
from registration import Conflict, RecoveryRequired, Rejected


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Explicit dummy credentials prevent metadata lookup or local AWS logins.
        cls.session = boto3.Session(aws_access_key_id="offline", aws_secret_access_key="offline",
                                    region_name="ap-southeast-2")

    def setUp(self):
        self.client = Mock()
        self.store = aws_adapter.DynamoStore(self.client, "offline-onboarding-table", clock=lambda: 10)

    def test_get_is_consistent_and_decodes_envelope(self):
        self.client.get_item.return_value = {"Item": {"revision": {"N": "2"}, "data": {"S": '{"x":1}'}}}
        self.assertEqual(self.store.get(("inventory", "lab:01")), {"revision": 2, "data": {"x": 1}})
        self.client.get_item.assert_called_once_with(TableName="offline-onboarding-table",
                                                    Key={"pk": {"S": "inventory"}, "sk": {"S": "lab:01"}},
                                                    ConsistentRead=True)
        self.client.get_item.return_value = {}
        self.assertIsNone(self.store.get(("registration", "missing")))

    def test_transaction_conditions_audit_and_sdk_shape(self):
        inventory = ("inventory", "lab:01")
        registration = ("registration", "a" * 64)
        self.store.commit({inventory: {"revision": 3, "data": {"approval": "APPROVED"}}, registration: None},
                          {registration: {"phase": "VALIDATING"}}, "lab:01", "VALIDATING", "registration")
        kwargs = self.client.transact_write_items.call_args.kwargs
        model = self.session.client("dynamodb").meta.service_model.operation_model("TransactWriteItems")
        validate_parameters(kwargs, model.input_shape)
        writes = kwargs["TransactItems"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0]["ConditionCheck"]["ConditionExpression"], "#revision = :revision AND #data = :data")
        self.assertEqual(writes[1]["Put"]["ConditionExpression"], "attribute_not_exists(pk)")
        self.assertEqual(writes[1]["Put"]["Item"]["revision"], {"N": "1"})
        self.assertEqual(writes[2]["Put"]["Item"]["pk"], {"S": "audit:lab:01"})
        self.assertEqual(len(kwargs["ClientRequestToken"]), 36)

    def test_update_increments_revision_and_disallows_unguarded_write(self):
        key = ("registration", "a" * 64)
        self.store.commit({key: {"revision": 2, "data": {"phase": "VALIDATING"}}},
                          {key: {"phase": "ATTACHING"}}, "lab:01", "ATTACHING", "registration")
        put = self.client.transact_write_items.call_args.kwargs["TransactItems"][0]["Put"]
        self.assertEqual(put["Item"]["revision"], {"N": "3"})
        with self.assertRaisesRegex(ValueError, "unguarded_write"):
            self.store.commit({}, {key: {}}, "lab:01", "ATTACHING", "registration")

    def test_conflict_is_distinguished_from_unknown_transaction_outcome(self):
        self.client.transact_write_items.side_effect = ClientError({
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}, "TransactWriteItems")
        with self.assertRaises(Conflict):
            self.store.commit({("registration", "a"): None}, {}, "lab:01", "VALIDATING", "registration")
        self.client.transact_write_items.side_effect = OSError("secret transport detail")
        with self.assertRaises(OSError):
            self.store.commit({("registration", "a"): None}, {}, "lab:01", "VALIDATING", "registration")

    def test_find_thing_only_normalizes_not_found(self):
        client = self.session.client("iot")
        adapter = aws_adapter.IoT(client)
        error = client.exceptions.ResourceNotFoundException({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeThing")
        with patch.object(client, "describe_thing", side_effect=error):
            self.assertIsNone(adapter.find_thing("onboard-test-01"))
        with patch.object(client, "describe_thing", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                adapter.find_thing("onboard-test-01")

    def test_exclusive_attachment_request_is_supported_by_sdk(self):
        model = self.session.client("iot").meta.service_model.operation_model("AttachThingPrincipal")
        validate_parameters({"thingName": "onboard-test-01", "principal": "arn:aws:iot:ap-southeast-2:111122223333:cert/" + "a" * 64,
                             "thingPrincipalType": "EXCLUSIVE_THING"}, model.input_shape)

    def test_disabled_handler_never_constructs_aws_clients(self):
        with patch.dict(os.environ, {"REGISTRATION_ENABLED": "false"}), patch.object(aws_adapter, "application") as app:
            self.assertEqual(aws_adapter.handler({}, None), {"state": "DISABLED"})
            app.assert_not_called()

    def test_handler_sanitizes_dependency_failures(self):
        for exception, expected in ((Conflict("internal"), "registration_recovery_required"),
                                    (RecoveryRequired("internal"), "registration_recovery_required"),
                                    (OSError("secret transport detail"), "registration_dependency_failure")):
            with patch.dict(os.environ, {"REGISTRATION_ENABLED": "true"}), patch.object(aws_adapter, "application") as app:
                app.return_value.handle.side_effect = exception
                with self.assertRaisesRegex(RuntimeError, "^" + expected + "$"):
                    aws_adapter.handler({"untrusted": "secret"}, None)

    def test_handler_returns_fixed_rejection(self):
        with patch.dict(os.environ, {"REGISTRATION_ENABLED": "true"}), patch.object(aws_adapter, "application") as app:
            app.return_value.handle.side_effect = Rejected("inventory_not_approved")
            with self.assertLogs("aws_adapter", level="WARNING") as logs:
                self.assertEqual(aws_adapter.handler({"untrusted": "secret"}, None),
                                 {"state": "REJECTED", "reason": "inventory_not_approved"})
            self.assertNotIn("secret", str(logs.output))

    def test_application_disables_sdk_retry_and_uses_new_table(self):
        env = {"AWS_REGION": "ap-southeast-2", "EXPECTED_ACCOUNT": "111122223333",
               "FACTORY_CA_ID": "a" * 64, "BOOTSTRAP_POLICY": "isolated-bootstrap",
               "ONBOARDING_TABLE": "isolated-table"}
        with patch.dict(os.environ, env), patch("boto3.client") as client:
            worker = aws_adapter.application()
            self.assertEqual(worker.store.table, "isolated-table")
            for call in client.call_args_list:
                self.assertEqual(call.kwargs["config"].retries["total_max_attempts"], 1)


if __name__ == "__main__":
    unittest.main()