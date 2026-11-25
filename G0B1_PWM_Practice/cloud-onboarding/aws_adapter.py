"""AWS adapter for the isolated registration worker; clients are lazy."""
import json
import logging
import os
import time
import uuid

from registration import Conflict, RecoveryRequired, Registration, Rejected


class DynamoStore:
    def __init__(self, client, table, clock=time.time):
        self.client, self.table, self.clock = client, table, clock

    @staticmethod
    def key(key):
        return {"pk": {"S": key[0]}, "sk": {"S": key[1]}}

    def get(self, key):
        item = self.client.get_item(TableName=self.table, Key=self.key(key), ConsistentRead=True).get("Item")
        if item is None:
            return None
        return {"revision": int(item["revision"]["N"]), "data": json.loads(item["data"]["S"])}

    def commit(self, expected, changes, owner, phase, actor):
        """CAS records and append audit in ONE DynamoDB transaction."""
        writes = []
        for key, old in expected.items():
            operation = {"TableName": self.table, "Key": self.key(key)}
            if old is None:
                operation["ConditionExpression"] = "attribute_not_exists(pk)"
            else:
                operation.update(ConditionExpression="#revision = :revision AND #data = :data",
                                 ExpressionAttributeNames={"#revision": "revision", "#data": "data"},
                                 ExpressionAttributeValues={":revision": {"N": str(old["revision"])},
                                                            ":data": {"S": self.encode(old["data"])}})
            if key in changes:
                operation["Item"] = dict(operation.pop("Key"),
                                         data={"S": self.encode(changes[key])},
                                         revision={"N": str(1 if old is None else old["revision"] + 1)})
                writes.append({"Put": operation})
            else:
                writes.append({"ConditionCheck": operation})
        if set(changes) - set(expected):
            raise ValueError("unguarded_write")
        audit = dict(self.key(("audit:" + owner, str(uuid.uuid4()))), revision={"N": "1"},
                     data={"S": self.encode({"phase": phase, "actor": actor, "time": self.clock()})})
        writes.append({"Put": {"TableName": self.table, "Item": audit,
                               "ConditionExpression": "attribute_not_exists(pk)"}})
        try:
            self.client.transact_write_items(TransactItems=writes, ClientRequestToken=str(uuid.uuid4()))
        except Exception as exc:
            response = getattr(exc, "response", {})
            if response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                    reason.get("Code") == "ConditionalCheckFailed" for reason in response.get("CancellationReasons", [])):
                raise Conflict("conditional_conflict") from None
            raise

    @staticmethod
    def encode(data):
        return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)


class IoT:
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    def find_thing(self, name):
        try:
            return self.client.describe_thing(thingName=name)
        except self.client.exceptions.ResourceNotFoundException:
            return None


def application():
    import boto3
    from botocore.config import Config

    # No automatic retries of state-changing IoT calls after an ambiguous result.
    config = Config(retries={"total_max_attempts": 1}, connect_timeout=3, read_timeout=5)
    region = os.environ["AWS_REGION"]
    return Registration(
        DynamoStore(boto3.client("dynamodb", region_name=region, config=config), os.environ["ONBOARDING_TABLE"]),
        IoT(boto3.client("iot", region_name=region, config=config)),
        os.environ["EXPECTED_ACCOUNT"], region, os.environ["FACTORY_CA_ID"],
        os.environ["BOOTSTRAP_POLICY"], time.time, os.environ.get("AWS_PARTITION", "aws"))


def handler(event, context):
    if os.environ.get("REGISTRATION_ENABLED") != "true":
        return {"state": "DISABLED"}
    try:
        return application().handle(event)
    except Rejected as exc:
        # Only fixed reason codes, never raw event data / SDK exception messages.
        logging.getLogger(__name__).warning("registration_rejected reason=%s", str(exc))
        return {"state": "REJECTED", "reason": str(exc)}
    except (Conflict, RecoveryRequired):
        raise RuntimeError("registration_recovery_required") from None
    except Exception:
        raise RuntimeError("registration_dependency_failure") from None