"""Offline infrastructure boundaries, dependency graph and policy contract."""
import json
import re
import unittest
from pathlib import Path

import yaml

from registration import LIVE_CERT


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise ValueError("duplicate_yaml_key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueLoader.add_constructor("tag:yaml.org,2002:map", unique_mapping)


def refs(value):
    result = set()
    if isinstance(value, list):
        for item in value:
            result.update(refs(item))
    elif isinstance(value, dict):
        if "Ref" in value:
            result.add(value["Ref"])
        if "Fn::GetAtt" in value:
            target = value["Fn::GetAtt"]
            result.add(target[0] if isinstance(target, list) else target.split(".")[0])
        if "Fn::Sub" in value:
            result.update(name.split(".")[0] for name in re.findall(r"\$\{([^!}]+)\}", value["Fn::Sub"]))
        for item in value.values():
            result.update(refs(item))
    return result


def statements(role, enabled):
    result = []
    for policy in role["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            if "Fn::If" in statement:
                statement = statement["Fn::If"][1 if enabled else 2]
            if statement != {"Ref": "AWS::NoValue"}:
                result.append(statement)
    return result


def actions(statement):
    value = statement["Action"]
    return value if isinstance(value, list) else [value]


class InfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = yaml.load(Path(__file__).with_name("template.yaml").read_text(), Loader=UniqueLoader)
        cls.resources = cls.template["Resources"]

    def props(self, name):
        return self.resources[name]["Properties"]

    def test_all_references_resolve_and_resource_graph_has_no_cycles(self):
        defined = set(self.resources) | set(self.template["Parameters"])
        for ref in refs(self.template):
            if not ref.startswith("AWS::"):
                self.assertIn(ref, defined)
        graph = {name: refs(resource) & set(self.resources) for name, resource in self.resources.items()}
        def visit(name, ancestors):
            self.assertNotIn(name, ancestors, "CloudFormation dependency cycle")
            for dependency in graph[name]:
                visit(dependency, ancestors | {name})
        for name in graph:
            visit(name, set())

    def test_retention_and_no_identity_expiry(self):
        for name in ("OnboardingTable", "BootstrapPolicy", "RegistrationLogGroup", "FailureQueue"):
            self.assertEqual(self.resources[name]["DeletionPolicy"], "Retain")
            self.assertEqual(self.resources[name]["UpdateReplacePolicy"], "Retain")
        table = self.props("OnboardingTable")
        self.assertTrue(table["DeletionProtectionEnabled"])
        self.assertTrue(table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"])
        self.assertTrue(table["SSESpecification"]["SSEEnabled"])
        self.assertNotIn("TimeToLiveSpecification", table)
        self.assertEqual(table["KeySchema"], [{"AttributeName": "pk", "KeyType": "HASH"},
                                             {"AttributeName": "sk", "KeyType": "RANGE"}])

    def test_default_disabled_at_rule_handler_and_mutation_iam(self):
        self.assertEqual(self.template["Parameters"]["RegistrationEnabled"]["Default"], "false")
        self.assertEqual(self.props("RegistrationRule")["TopicRulePayload"]["RuleDisabled"],
                         {"Fn::If": ["RegistrationAllowed", False, True]})
        self.assertEqual(self.props("RegistrationFunction")["Environment"]["Variables"]["REGISTRATION_ENABLED"],
                         {"Ref": "RegistrationEnabled"})
        disabled = [a for s in statements(self.props("RegistrationRole"), False) if s["Effect"] == "Allow" for a in actions(s)]
        enabled = [a for s in statements(self.props("RegistrationRole"), True) if s["Effect"] == "Allow" for a in actions(s)]
        for action in ("iot:CreateThing", "iot:AttachThingPrincipal", "iot:AttachPolicy", "iot:UpdateCertificate"):
            self.assertNotIn(action, disabled)
            self.assertIn(action, enabled)

    def test_no_ota_or_admin_or_ca_creation_rights(self):
        allowed = [a for s in statements(self.props("RegistrationRole"), True) if s["Effect"] == "Allow" for a in actions(s)]
        self.assertFalse(any(a.startswith(("s3:", "kms:", "cognito")) for a in allowed))
        for action in ("iot:Publish", "iot:CreatePolicy", "iot:RegisterCACertificate", "iot:DeleteCertificate",
                       "iot:CreateCertificateFromCsr", "dynamodb:Scan", "dynamodb:UpdateItem", "dynamodb:DeleteItem"):
            self.assertNotIn(action, allowed)
        types = {r["Type"] for r in self.resources.values()}
        self.assertNotIn("AWS::IoT::Thing", types)
        self.assertFalse(any(t.startswith(("AWS::Cognito", "AWS::ApiGateway", "AWS::KMS", "AWS::S3")) for t in types))

    def test_worker_cannot_write_approval_inventory(self):
        writes = [s for s in statements(self.props("RegistrationRole"), True) if "dynamodb:PutItem" in actions(s)]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["Resource"], {"Fn::GetAtt": ["OnboardingTable", "Arn"]})
        self.assertEqual(writes[0]["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"],
                         ["registration", "audit:*"])

    def test_live_identity_is_explicitly_denied(self):
        deny = [s for s in statements(self.props("RegistrationRole"), True) if s["Effect"] == "Deny"]
        self.assertEqual(len(deny), 1)
        self.assertEqual(deny[0]["Action"], "iot:*")
        self.assertIn(LIVE_CERT, json.dumps(deny))
        self.assertIn("thing/pi-lab-01", json.dumps(deny))

    def test_certificate_and_thing_iam_limits_are_explicit(self):
        stmts = {s["Sid"]: s for s in statements(self.props("RegistrationRole"), True)}
        attachment = stmts["AttachOnlyToTestThings"]
        self.assertTrue(attachment["Resource"]["Fn::Sub"].endswith(":cert/*"))
        arns = attachment["Condition"]["ArnEquals"]["iot:thingArn"]
        self.assertEqual([a["Fn::Sub"].split(":thing/")[1] for a in arns], ["onboard-test-01", "onboard-test-02"])
        self.assertTrue(stmts["ActivateAndAttachBootstrap"]["Resource"]["Fn::Sub"].endswith(":cert/*"))
        # Verified against the AWS service authorization reference: this List
        # operation has no resource type, unlike ListPrincipalThings.
        self.assertEqual(stmts["ReadAttachedPolicies"]["Action"], "iot:ListAttachedPolicies")
        self.assertEqual(stmts["ReadAttachedPolicies"]["Resource"], "*")
        for stmt in stmts.values():
            if stmt["Effect"] == "Allow" and any(a.startswith("iot:") for a in actions(stmt)):
                self.assertEqual(stmt["Condition"]["StringEquals"]["aws:RequestedRegion"], {"Ref": "AWS::Region"})

    def test_rule_selects_only_configured_ca_and_exact_invocation(self):
        payload = self.props("RegistrationRule")["TopicRulePayload"]
        self.assertEqual(payload["Sql"], {"Fn::Sub": "SELECT * FROM '$aws/events/certificates/registered/${FactoryCaId}'"})
        permission = self.props("AllowExactRegistrationRule")
        self.assertEqual(permission["Principal"], "iot.amazonaws.com")
        self.assertEqual(permission["SourceAccount"], {"Ref": "AWS::AccountId"})
        self.assertEqual(permission["SourceArn"], {"Fn::GetAtt": ["RegistrationRule", "Arn"]})
        trust = self.props("RuleFailureRole")["AssumeRolePolicyDocument"]["Statement"][0]
        self.assertIn("aws:SourceAccount", trust["Condition"]["StringEquals"])
        self.assertIn("${RegistrationRuleName}", trust["Condition"]["ArnEquals"]["aws:SourceArn"]["Fn::Sub"])

    def test_failures_are_bounded_and_captured(self):
        self.assertEqual(self.props("RegistrationFunction")["ReservedConcurrentExecutions"], 1)
        retry = self.props("RegistrationRetries")
        self.assertEqual(retry["MaximumRetryAttempts"], 2)
        self.assertEqual(retry["MaximumEventAgeInSeconds"], 300)
        self.assertEqual(retry["DestinationConfig"]["OnFailure"]["Destination"], {"Fn::GetAtt": ["FailureQueue", "Arn"]})
        self.assertIn("ErrorAction", self.props("RegistrationRule")["TopicRulePayload"])
        self.assertTrue(self.props("FailureQueue")["SqsManagedSseEnabled"])
        self.assertEqual(self.resources["RecoveryQueueAlarm"]["Type"], "AWS::CloudWatch::Alarm")
        self.assertEqual(self.resources["RegistrationErrorAlarm"]["Type"], "AWS::CloudWatch::Alarm")

    def test_bootstrap_has_only_own_onboarding_topics_and_client_id_check(self):
        policy = self.props("BootstrapPolicy")["PolicyDocument"]["Statement"]
        self.assertEqual([s["Action"] for s in policy], ["iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive"])
        connect = policy[0]
        self.assertEqual(connect["Condition"]["Bool"], {"iot:Connection.Thing.IsAttached": "true"})
        self.assertEqual(connect["Condition"]["StringEquals"], {"iot:ClientId": "${iot:Connection.Thing.ThingName}"})
        for thing in ("onboard-test-01", "onboard-test-02"):
            other = "onboard-test-02" if thing.endswith("01") else "onboard-test-01"
            for statement in policy[1:]:
                raw = statement["Resource"]["Fn::Sub"]
                self.assertNotIn("*", raw)
                self.assertNotIn("lab/", raw)
                self.assertNotIn("/confirm", raw)
                topic = raw.replace("${!iot:Connection.Thing.ThingName}", thing)
                self.assertIn("/" + thing + "/", topic)
                self.assertNotIn("/" + other + "/", topic)
        self.assertEqual(len(connect["Resource"]), 2)

    def test_artifact_pins_object_version_and_no_private_ca_parameters(self):
        self.assertEqual(self.props("RegistrationFunction")["Code"]["S3ObjectVersion"], {"Ref": "ArtifactVersion"})
        parameters = self.template["Parameters"]
        self.assertFalse(any("private" in p.lower() or "secret" in p.lower() for p in parameters))
        pattern = parameters["ArtifactBucket"]["AllowedPattern"]
        self.assertIsNone(re.fullmatch(pattern, "iot-lab-firmware-844274146091"))


if __name__ == "__main__":
    unittest.main()