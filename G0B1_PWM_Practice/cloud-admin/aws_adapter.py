"""Small boto3 adapter. Store JSON as a string to preserve numeric types exactly."""
import json
import os

from backend import Admin, Conflict, canonical


class DynamoStore:
    def __init__(self, client, table):
        self.client, self.table = client, table

    @staticmethod
    def key(kind, identifier):
        return {"pk": {"S": kind}, "sk": {"S": identifier}}

    @staticmethod
    def decode(item):
        if not item:
            return None
        row = json.loads(item["data"]["S"])
        row["_revision"] = int(item["revision"]["N"])
        return row

    def get(self, kind, identifier):
        return self.decode(self.client.get_item(TableName=self.table, Key=self.key(kind, identifier),
                                                ConsistentRead=True).get("Item"))

    def list(self, kind):
        rows, cursor = [], None
        while True:
            params = dict(TableName=self.table, KeyConditionExpression="pk = :kind",
                          ExpressionAttributeValues={":kind": {"S": kind}}, ConsistentRead=True)
            if cursor:
                params["ExclusiveStartKey"] = cursor
            result = self.client.query(**params)
            rows.extend(self.decode(item) for item in result.get("Items", []))
            cursor = result.get("LastEvaluatedKey")
            if not cursor:
                return rows

    def commit(self, changes):
        operations = []
        for kind, identifier, before, after in changes:
            common = dict(TableName=self.table)
            if before is None:
                common["ConditionExpression"] = "attribute_not_exists(pk)"
                revision = 1
            else:
                revision = before["_revision"] + 1
                common.update(ConditionExpression="revision = :revision",
                              ExpressionAttributeValues={":revision": {"N": str(before["_revision"])}})
            if after is before and before is not None:
                common["Key"] = self.key(kind, identifier)
                operations.append({"ConditionCheck": common})
            elif after is None:
                common["Key"] = self.key(kind, identifier)
                operations.append({"Delete": common})
            else:
                clean = {k: v for k, v in after.items() if k != "_revision"}
                common["Item"] = dict(self.key(kind, identifier), revision={"N": str(revision)},
                                       data={"S": canonical(clean).decode()})
                operations.append({"Put": common})
        try:
            self.client.transact_write_items(TransactItems=operations)
        except Exception as exc:
            response = getattr(exc, "response", {})
            reasons = response.get("CancellationReasons", [])
            if (response.get("Error", {}).get("Code") == "TransactionCanceledException" and
                    any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)):
                raise Conflict() from None
            raise


def application():
    import boto3
    from botocore.config import Config
    # No SDK retry may republish a command after a lost HTTP response.
    once = Config(retries={"total_max_attempts": 1, "mode": "standard"},
                  connect_timeout=3, read_timeout=5)
    region = os.environ["AWS_REGION"]
    return Admin(DynamoStore(boto3.client("dynamodb"), os.environ["TABLE_NAME"]),
                 boto3.client("s3", config=Config(signature_version="s3v4")), boto3.client("kms"),
                 boto3.client("iot-data", endpoint_url="https://" + os.environ["IOT_DATA_ENDPOINT"], config=once),
                 os.environ["FIRMWARE_BUCKET"], region, os.environ["SIGNING_KEY_ARN"],
                 os.environ["COGNITO_ISSUER"], os.environ["COGNITO_CLIENT_ID"])