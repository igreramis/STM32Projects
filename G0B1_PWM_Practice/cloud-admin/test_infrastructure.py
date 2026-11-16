"""Offline infrastructure invariants; no AWS SDK, credentials, or application imports."""
import io
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import smoke_test

try:
    import yaml
except ImportError:
    yaml = None


def values(value):
    return value if isinstance(value, list) else [value]


def references(value):
    """Find resource, parameter and condition dependencies (including Fn::Sub)."""
    result = set()
    if isinstance(value, list):
        for item in value:
            result.update(references(item))
    elif isinstance(value, dict):
        if "Ref" in value:
            result.add(value["Ref"])
        if "Fn::If" in value:
            result.add(value["Fn::If"][0])
        if isinstance(value.get("Condition"), str):
            result.add(value["Condition"])
        if "Fn::GetAtt" in value:
            attribute = value["Fn::GetAtt"]
            result.add(attribute[0] if isinstance(attribute, list) else attribute.split(".")[0])
        if "Fn::Sub" in value:
            substitution = value["Fn::Sub"]
            text, bindings = (substitution, {}) if isinstance(substitution, str) else substitution
            for name in re.findall(r"\$\{([^}!]+)\}", text):
                if name not in bindings:
                    result.add(name.split(".")[0])
            result.update(references(bindings))
        for item in value.values():
            result.update(references(item))
    return result


def condition_references(value):
    """Condition names have their own namespace; IAM Condition maps are not names."""
    result = set()
    if isinstance(value, list):
        for item in value:
            result.update(condition_references(item))
    elif isinstance(value, dict):
        if "Fn::If" in value:
            result.add(value["Fn::If"][0])
        if isinstance(value.get("Condition"), str):
            result.add(value["Condition"])
        for item in value.values():
            result.update(condition_references(item))
    return result


NO_VALUE = object()


def resolve_conditions(value, parameters, conditions):
    """Evaluate this template's condition intrinsics, removing AWS::NoValue."""
    if isinstance(value, list):
        resolved = [resolve_conditions(item, parameters, conditions) for item in value]
        return [item for item in resolved if item is not NO_VALUE]
    if isinstance(value, dict):
        if value == {"Ref": "AWS::NoValue"}:
            return NO_VALUE
        if "Ref" in value and value["Ref"] in parameters:
            return parameters[value["Ref"]]
        if "Fn::Equals" in value:
            left, right = resolve_conditions(value["Fn::Equals"], parameters, conditions)
            return left == right
        if "Fn::If" in value:
            name, enabled, disabled = value["Fn::If"]
            branch = enabled if resolve_conditions(conditions[name], parameters, conditions) else disabled
            return resolve_conditions(branch, parameters, conditions)
        resolved = {key: resolve_conditions(item, parameters, conditions) for key, item in value.items()}
        return {key: item for key, item in resolved.items() if item is not NO_VALUE}
    return value


@unittest.skipIf(yaml is None, "PyYAML is not installed; template validation NOT performed")
class InfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reject duplicate keys rather than silently losing a security setting.
        class UniqueLoader(yaml.SafeLoader):
            pass

        def unique_mapping(loader, node):
            mapping = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                if key in mapping:
                    raise ValueError("Duplicate YAML key: " + str(key))
                mapping[key] = loader.construct_object(value_node, deep=True)
            return mapping

        UniqueLoader.add_constructor("tag:yaml.org,2002:map", unique_mapping)
        cls.template = yaml.load(Path(__file__).with_name("template.yaml").read_text(), Loader=UniqueLoader)
        cls.resources = cls.template["Resources"]

    def props(self, name):
        return self.resources[name]["Properties"]

    def of_type(self, resource_type):
        return {name: item for name, item in self.resources.items() if item["Type"] == resource_type}

    def statements(self, role, enabled):
        statements = [statement for policy in self.props(role)["Policies"]
                      for statement in policy["PolicyDocument"]["Statement"]]
        return resolve_conditions(statements, {"FirmwareDeploymentEnabled": enabled},
                                  self.template["Conditions"])

    def test_retained_table_and_signing_key(self):
        for name in ("AdminTable", "SigningKey"):
            self.assertEqual(self.resources[name]["DeletionPolicy"], "Retain")
            self.assertEqual(self.resources[name]["UpdateReplacePolicy"], "Retain")
        table = self.props("AdminTable")
        self.assertEqual(table["BillingMode"], "PAY_PER_REQUEST")
        self.assertEqual(table["AttributeDefinitions"], [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"}])
        self.assertEqual(table["KeySchema"], [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"}])
        self.assertTrue(table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"])
        self.assertEqual(self.props("SigningKey")["KeySpec"], "RSA_2048")
        self.assertEqual(self.props("SigningKey")["KeyUsage"], "SIGN_VERIFY")

    def test_public_code_client_scope_and_order(self):
        client = self.props("UserPoolClient")
        self.assertIs(client["GenerateSecret"], False)
        self.assertIs(client["AllowedOAuthFlowsUserPoolClient"], True)
        self.assertEqual(client["AllowedOAuthFlows"], ["code"])
        self.assertEqual(set(client["AllowedOAuthScopes"]), {"openid", "profile", "admin-api/access"})
        self.assertEqual(client["ExplicitAuthFlows"], ["ALLOW_REFRESH_TOKEN_AUTH"])
        self.assertIn("AdminResourceServer", values(self.resources["UserPoolClient"]["DependsOn"]))
        self.assertEqual(self.props("AdminResourceServer")["Identifier"], "admin-api")
        self.assertEqual(self.props("AdminResourceServer")["Scopes"][0]["ScopeName"], "access")
        self.assertEqual(self.props("AdminsGroup")["GroupName"], "admins")
        self.assertTrue(self.props("UserPool")["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"])
        self.assertEqual(self.props("UserPoolDomain")["Domain"],
                         {"Fn::Sub": "admin-api-${AWS::AccountId}-${AWS::Region}"})

    def test_data_routes_require_scoped_jwt_and_preflight_is_separate(self):
        routes = self.of_type("AWS::ApiGatewayV2::Route")
        self.assertEqual(set(routes), {"ApiProxyRoute", "ApiPreflightRoute"})
        preflight = self.props("ApiPreflightRoute")
        self.assertEqual(preflight["RouteKey"], "OPTIONS /api/{proxy+}")
        self.assertEqual(preflight["AuthorizationType"], "NONE")
        self.assertEqual(preflight["Target"], {"Fn::Sub": "integrations/${HttpIntegration}"})
        self.assertNotIn("AuthorizerId", preflight)
        self.assertNotIn("AuthorizationScopes", preflight)
        for route in [routes["ApiProxyRoute"]]:
            props = route["Properties"]
            self.assertEqual(props["RouteKey"], "ANY /api/{proxy+}")
            self.assertEqual(props["AuthorizationType"], "JWT")
            self.assertEqual(props["AuthorizationScopes"], ["admin-api/access"])
            self.assertEqual(props["AuthorizerId"], {"Ref": "JwtAuthorizer"})
            self.assertEqual(props["Target"], {"Fn::Sub": "integrations/${HttpIntegration}"})
        auth = self.props("JwtAuthorizer")
        self.assertEqual(auth["AuthorizerType"], "JWT")
        self.assertEqual(auth["JwtConfiguration"]["Audience"], [{"Ref": "UserPoolClient"}])
        self.assertEqual(auth["JwtConfiguration"]["Issuer"],
                         self.props("HttpFunction")["Environment"]["Variables"]["COGNITO_ISSUER"])
        self.assertEqual(self.props("HttpIntegration")["IntegrationUri"], {"Fn::GetAtt": ["HttpFunction", "Arn"]})
        self.assertEqual(self.props("HttpIntegration")["PayloadFormatVersion"], "2.0")
        self.assertFalse(self.of_type("AWS::Lambda::Url"))

    def test_cors_and_existing_lab_parameters(self):
        parameters = self.template["Parameters"]
        self.assertEqual(parameters["WebOrigin"]["Default"], "http://localhost:5173")
        cors = self.props("HttpApi")["CorsConfiguration"]
        self.assertEqual(cors["AllowOrigins"], [{"Ref": "WebOrigin"}])
        self.assertEqual(set(cors["AllowHeaders"]), {"authorization", "content-type"})
        self.assertEqual(set(cors["AllowMethods"]), {"GET", "POST", "OPTIONS"})
        self.assertEqual(parameters["FirmwareBucket"]["Default"], "iot-lab-firmware-844274146091")
        self.assertEqual(parameters["IotDataEndpoint"]["Default"],
                         "an0l72m95csqi-ats.iot.ap-southeast-2.amazonaws.com")
        self.assertNotIn("Default", parameters["ArtifactBucket"])
        self.assertNotIn("Default", parameters["ArtifactKey"])

    def test_roles_have_only_required_actions_and_resources(self):
        self.assertEqual(set(self.of_type("AWS::IAM::Role")), {"HttpRole", "IngestRole"})
        self.assertFalse(self.of_type("AWS::IAM::Policy"))
        self.assertFalse(self.of_type("AWS::IAM::ManagedPolicy"))
        for enabled in ("false", "true"):
            for role, log in (("HttpRole", "HttpLogGroup"), ("IngestRole", "IngestLogGroup")):
                with self.subTest(enabled=enabled, role=role):
                    self.assertNotIn("ManagedPolicyArns", self.props(role))
                    table_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
                                     "dynamodb:ConditionCheckItem"]
                    if role == "HttpRole":
                        table_actions.insert(1, "dynamodb:Query")
                    expected = [{
                        "Sid": "TableOperations" if role == "HttpRole" else "IngestTableOperations",
                        "Effect": "Allow", "Action": table_actions,
                        "Resource": {"Fn::GetAtt": ["AdminTable", "Arn"]}}, {
                        "Sid": "WriteOwnLogs", "Effect": "Allow",
                        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                        "Resource": {"Fn::GetAtt": [log, "Arn"]}}]
                    if role == "HttpRole":
                        expected.extend([{
                            "Sid": "VersionedFirmwareObjectsOnly", "Effect": "Allow",
                            "Action": ["s3:GetObjectVersion", "s3:PutObject"],
                            "Resource": {"Fn::Sub": "arn:${AWS::Partition}:s3:::${FirmwareBucket}/firmware/stm32g0b1/*"}}, {
                            "Sid": "SignReleasesOnly", "Effect": "Allow", "Action": "kms:Sign",
                            "Resource": {"Fn::GetAtt": ["SigningKey", "Arn"]},
                            "Condition": {"StringEquals": {
                                "kms:SigningAlgorithm": "RSASSA_PKCS1_V1_5_SHA_256"}}}])
                        if enabled == "true":
                            expected.append({
                                "Sid": "PublishFirmwareCommandOnly", "Effect": "Allow",
                                "Action": "iot:Publish", "Resource": {"Fn::Sub":
                                    "arn:${AWS::Partition}:iot:${AWS::Region}:${AWS::AccountId}:topic/lab/pi-lab-01/commands"}})
                    self.assertCountEqual(self.statements(role, enabled), expected)

    def test_deployment_is_default_off_and_publish_statement_is_conditional(self):
        parameter = self.template["Parameters"]["FirmwareDeploymentEnabled"]
        self.assertEqual(parameter["Type"], "String")
        self.assertEqual(parameter["Default"], "false")
        self.assertEqual(parameter["AllowedValues"], ["false", "true"])
        self.assertEqual(self.template["Conditions"], {"FirmwareDeploymentAllowed": {
            "Fn::Equals": [{"Ref": "FirmwareDeploymentEnabled"}, "true"]}})
        statements = [s for p in self.props("HttpRole")["Policies"]
                      for s in p["PolicyDocument"]["Statement"]]
        conditional = [s for s in statements if "Fn::If" in s]
        self.assertEqual(conditional, [{"Fn::If": ["FirmwareDeploymentAllowed", {
            "Sid": "PublishFirmwareCommandOnly", "Effect": "Allow", "Action": "iot:Publish",
            "Resource": {"Fn::Sub":
                "arn:${AWS::Partition}:iot:${AWS::Region}:${AWS::AccountId}:topic/lab/pi-lab-01/commands"}},
            {"Ref": "AWS::NoValue"}]}])
        for enabled in ("false", "true"):
            with self.subTest(enabled=enabled):
                env = resolve_conditions(self.props("HttpFunction")["Environment"]["Variables"],
                                         {"FirmwareDeploymentEnabled": enabled}, self.template["Conditions"])
                self.assertEqual(env["FIRMWARE_DEPLOYMENT_ENABLED"], enabled)

    def test_functions_artifacts_environment_and_logs(self):
        self.assertEqual(set(self.of_type("AWS::Lambda::Function")), {"HttpFunction", "IngestFunction"})
        self.assertEqual(set(self.of_type("AWS::Logs::LogGroup")), {"HttpLogGroup", "IngestLogGroup"})
        for function, role, log in (("HttpFunction", "HttpRole", "HttpLogGroup"),
                                    ("IngestFunction", "IngestRole", "IngestLogGroup")):
            props = self.props(function)
            self.assertEqual(props["Runtime"], "python3.12")
            self.assertEqual(props["Handler"], "backend.handler")
            self.assertEqual(props["Role"], {"Fn::GetAtt": [role, "Arn"]})
            self.assertEqual(props["Code"], {"S3Bucket": {"Ref": "ArtifactBucket"},
                                              "S3Key": {"Ref": "ArtifactKey"}})
            self.assertEqual(props["LoggingConfig"]["LogGroup"], {"Ref": log})
            self.assertEqual(self.props(log)["RetentionInDays"], 14)
            env = props["Environment"]["Variables"]
            expected = {"TABLE_NAME", "FIRMWARE_BUCKET", "IOT_DATA_ENDPOINT",
                        "SIGNING_KEY_ARN", "COGNITO_ISSUER", "COGNITO_CLIENT_ID"}
            if function == "HttpFunction":
                expected.add("FIRMWARE_DEPLOYMENT_ENABLED")
                self.assertEqual(env["FIRMWARE_DEPLOYMENT_ENABLED"], {"Ref": "FirmwareDeploymentEnabled"})
            self.assertEqual(set(env), expected)
            self.assertEqual(env["TABLE_NAME"], {"Ref": "AdminTable"})
            self.assertEqual(env["COGNITO_CLIENT_ID"], {"Ref": "UserPoolClient"})

    def test_iot_sql_has_exact_projection_and_broker_envelope(self):
        expected_fields = {
            "TelemetryRule": {"device_id", "timestamp", "boot_id", "telemetry_count", "subdevices"},
            "AckRule": {"device_id", "command_id", "event_seq", "version", "status", "phase",
                        "flash_verified", "application_health_verified", "error"},
        }
        self.assertEqual(set(self.of_type("AWS::IoT::TopicRule")), set(expected_fields))
        for rule, fields in expected_fields.items():
            kind, suffix = ("telemetry", "telemetry") if rule == "TelemetryRule" else ("ack", "acks")
            payload = self.props(rule)["TopicRulePayload"]
            self.assertEqual(payload["AwsIotSqlVersion"], "2016-03-23")
            self.assertIs(payload["RuleDisabled"], False)
            sql = " ".join(payload["Sql"].split())
            match = re.fullmatch(r"SELECT (.+) FROM 'lab/pi-lab-01/" + suffix + "'", sql)
            self.assertIsNotNone(match)
            projected = [field.strip() for field in match.group(1).split(",")]
            expected = fields | {
                "'" + kind + "' AS ingest_type", "topic() AS ingest_topic",
                "clientid() AS ingest_client_id", "timestamp() AS ingest_received_at"}
            self.assertEqual(set(projected), expected)
            self.assertEqual(len(projected), len(expected))
            self.assertEqual(payload["Actions"], [{"Lambda": {
                "FunctionArn": {"Fn::GetAtt": ["IngestFunction", "Arn"]}}}])

    def test_invoke_permissions_are_exact_and_separated(self):
        self.assertEqual(set(self.of_type("AWS::Lambda::Permission")),
                         {"HttpInvokePermission", "TelemetryInvokePermission", "AckInvokePermission"})
        for name, rule in (("TelemetryInvokePermission", "TelemetryRule"), ("AckInvokePermission", "AckRule")):
            permission = self.props(name)
            self.assertEqual(permission, {
                "Action": "lambda:InvokeFunction", "FunctionName": {"Ref": "IngestFunction"},
                "Principal": "iot.amazonaws.com", "SourceAccount": {"Ref": "AWS::AccountId"},
                "SourceArn": {"Fn::GetAtt": [rule, "Arn"]}})
        self.assertEqual(self.props("HttpInvokePermission"), {
            "Action": "lambda:InvokeFunction", "FunctionName": {"Ref": "HttpFunction"},
            "Principal": "apigateway.amazonaws.com", "SourceAccount": {"Ref": "AWS::AccountId"},
            "SourceArn": {"Fn::Sub": "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${HttpApi}/$default/*/api/*"}})

    def test_outputs_and_backend_only_scope(self):
        self.assertTrue({"ApiUrl", "CognitoIssuer", "ClientId", "CognitoDomain", "SigningKeyArn",
                         "TableName", "HttpFunctionName", "IngestFunctionName"} <= set(self.template["Outputs"]))
        allowed_types = {"AWS::DynamoDB::Table", "AWS::KMS::Key", "AWS::Cognito::UserPool",
                         "AWS::Cognito::UserPoolGroup", "AWS::Cognito::UserPoolResourceServer",
                         "AWS::Cognito::UserPoolClient", "AWS::Cognito::UserPoolDomain",
                         "AWS::Logs::LogGroup", "AWS::IAM::Role", "AWS::Lambda::Function",
                         "AWS::ApiGatewayV2::Api", "AWS::ApiGatewayV2::Authorizer",
                         "AWS::ApiGatewayV2::Integration", "AWS::ApiGatewayV2::Route",
                         "AWS::ApiGatewayV2::Stage", "AWS::Lambda::Permission", "AWS::IoT::TopicRule"}
        self.assertTrue({r["Type"] for r in self.resources.values()} <= allowed_types)

    def test_references_resolve_and_dependency_graph_is_acyclic(self):
        conditions = self.template.get("Conditions", {})
        known = set(self.resources) | set(self.template["Parameters"]) | set(conditions)
        for reference in references(self.template):
            self.assertTrue(reference in known or reference.startswith("AWS::"), reference)
        self.assertTrue(condition_references(self.template) <= set(conditions))
        self.assertEqual(references(conditions["FirmwareDeploymentAllowed"]), {"FirmwareDeploymentEnabled"})
        self.assertIn("FirmwareDeploymentAllowed", references(self.resources["HttpRole"]))
        graph = {}
        for name, condition in conditions.items():
            self.assertFalse(references(condition) & set(self.resources), "Conditions cannot depend on resources")
            graph[name] = references(condition) & set(conditions)
        for name, resource in self.resources.items():
            explicit = set(values(resource.get("DependsOn", [])))
            self.assertTrue(explicit <= set(self.resources))
            graph[name] = (references(resource) & (set(self.resources) | set(conditions))) | explicit
        completed, active = set(), set()

        def visit(name):
            self.assertNotIn(name, active, "CloudFormation dependency cycle at " + name)
            if name in completed:
                return
            active.add(name)
            for dependency in graph[name]:
                visit(dependency)
            active.remove(name)
            completed.add(name)

        for name in graph:
            visit(name)


class ReferenceTests(unittest.TestCase):
    def test_conditional_dependencies_include_both_branches_and_ignore_iam_conditions(self):
        expression = {"Fn::If": ["PublishAllowed", {"Ref": "EnabledResource"},
                                {"Ref": "DisabledResource"}]}
        self.assertEqual(references(expression), {"PublishAllowed", "EnabledResource", "DisabledResource"})
        self.assertEqual(condition_references(expression), {"PublishAllowed"})
        expression = {"Fn::And": [{"Condition": "OtherCondition"},
                                  {"Fn::Equals": [{"Ref": "EnabledParameter"}, "true"]}]}
        self.assertEqual(references(expression), {"OtherCondition", "EnabledParameter"})
        self.assertEqual(condition_references(expression), {"OtherCondition"})
        self.assertEqual(condition_references({"Condition": {"StringEquals": {"key": "value"}}}), set())


class SmokeSafetyTests(unittest.TestCase):
    def run_smoke(self, parameter, runtime):
        """Exercise main with all AWS, HTTP and process effects mocked offline."""
        origin = "https://ui.invalid"
        outputs = {"ApiUrl": "https://api.invalid", "HttpFunctionName": "http-function",
                   "IngestFunctionName": "ingest-function", "UserPoolId": "pool",
                   "ClientId": "client", "CognitoIssuer": "https://issuer.invalid",
                   "SigningKeyArn": "signing-key"}
        parameters = {"WebOrigin": origin, "CallbackUrl": origin + "/", "LogoutUrl": origin + "/"}
        if parameter is not None:
            parameters["FirmwareDeploymentEnabled"] = parameter
        invocations = []

        def fake_aws(*args):
            operation = args[:2]
            if operation == ("sts", "get-caller-identity"):
                return {"Account": "844274146091"}
            if operation == ("cloudformation", "describe-stacks"):
                return {"Stacks": [{"StackStatus": "UPDATE_COMPLETE",
                    "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
                    "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]}]}
            if operation == ("lambda", "get-function-configuration"):
                self.assertEqual(args[3], outputs["HttpFunctionName"])
                return {} if runtime is None else {"Environment": {
                    "Variables": {"FIRMWARE_DEPLOYMENT_ENABLED": runtime}}}
            if operation == ("cognito-idp", "describe-user-pool-client"):
                return {"UserPoolClient": {"CallbackURLs": [origin + "/"], "LogoutURLs": [origin + "/"],
                    "AllowedOAuthFlows": ["code"], "AllowedOAuthScopes": ["openid", "profile", "admin-api/access"]}}
            if operation == ("lambda", "invoke"):
                event = json.loads(args[args.index("--payload") + 1])
                invocations.append((args[3], event))
                if args[3] == outputs["IngestFunctionName"]:
                    response = {"ok": False, "error": "event_rejected"}
                elif event["requestContext"]["http"]["method"] == "POST":
                    response = {"statusCode": 403, "body": json.dumps({"error": "deployment_disabled"})}
                else:
                    response = {"statusCode": 200, "body": json.dumps({event["rawPath"].split("/")[-1]: []})}
                Path(args[-1]).write_text(json.dumps(response))
                return {}
            if operation == ("kms", "get-public-key"):
                return {"KeySpec": "RSA_2048", "KeyUsage": "SIGN_VERIFY"}
            raise AssertionError("Unexpected AWS operation: " + str(operation))

        def fake_urlopen(request, timeout):
            if isinstance(request, str):
                if request.startswith(outputs["ApiUrl"]):
                    raise smoke_test.urllib.error.HTTPError(request, 401, "Unauthorized", {}, None)
                self.assertEqual(request, outputs["CognitoIssuer"] + "/.well-known/openid-configuration")
                return io.BytesIO(json.dumps({"issuer": outputs["CognitoIssuer"]}).encode())
            self.assertEqual(request.get_method(), "OPTIONS")
            response = Mock(status=204)
            response.headers = {"Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                                "Access-Control-Allow-Headers": "authorization,content-type"}
            if request.get_header("Origin") == origin:
                response.headers["Access-Control-Allow-Origin"] = origin
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            return context

        with patch.object(smoke_test, "aws", side_effect=fake_aws), \
                patch.object(smoke_test.urllib.request, "urlopen", side_effect=fake_urlopen), \
                patch("subprocess.run", side_effect=AssertionError("process forbidden")), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("builtins.print"):
            smoke_test.main()
        reads = [event for _, event in invocations
                 if event.get("requestContext", {}).get("http", {}).get("method") == "GET"]
        self.assertEqual(len(reads), 3)
        return [event for _, event in invocations
                if event.get("requestContext", {}).get("http", {}).get("method") == "POST"]

    def test_enabled_stack_or_runtime_never_invokes_deployment(self):
        for parameter, runtime in (("true", "true"), ("false", "true"), ("true", "false"), (None, "true")):
            with self.subTest(parameter=parameter, runtime=runtime):
                self.assertEqual(self.run_smoke(parameter, runtime), [])

    def test_unknown_disablement_never_invokes_deployment(self):
        for parameter, runtime in (("false", None), (None, None), ("false", "invalid"), ("invalid", "false")):
            with self.subTest(parameter=parameter, runtime=runtime):
                self.assertEqual(self.run_smoke(parameter, runtime), [])

    def test_disabled_and_legacy_stacks_keep_existing_disabled_check(self):
        for parameter in ("false", None):
            with self.subTest(parameter=parameter):
                posts = self.run_smoke(parameter, "false")
                self.assertEqual([post["rawPath"] for post in posts],
                                 ["/api/deployment-arms", "/api/deployments"])
                self.assertTrue(all(post["body"] == "{}" for post in posts))


if __name__ == "__main__":
    unittest.main()