"""Read-only live backend checks. No MQTT publishing, firmware upload or user creation.

IAM-authorized Lambda invocation tests the runtime separately from public JWT
enforcement; it is NOT proof of an end-user Cognito login.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request


AWS = str(Path.home() / ".local/bin/aws")


def aws(*args):
    result = subprocess.run(
        [AWS, *args, "--profile", "ota-admin", "--region", "ap-southeast-2",
         "--no-cli-pager", "--output", "json"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def main():
    assert aws("sts", "get-caller-identity")["Account"] == "844274146091"
    stack = aws("cloudformation", "describe-stacks", "--stack-name", "stm32-admin-backend")["Stacks"][0]
    assert stack["StackStatus"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    parameters = {item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]}
    origin = parameters["WebOrigin"]
    configuration = aws("lambda", "get-function-configuration", "--function-name",
                        outputs["HttpFunctionName"])
    deployment_state = configuration.get("Environment", {}).get("Variables", {}).get(
        "FIRMWARE_DEPLOYMENT_ENABLED")

    def invoke(name, event):
        with tempfile.TemporaryDirectory(prefix="admin-smoke-") as directory:
            path = Path(directory) / "response.json"
            result = aws("lambda", "invoke", "--function-name", outputs[name],
                         "--cli-binary-format", "raw-in-base64-out", "--payload",
                         json.dumps(event), str(path))
            assert "FunctionError" not in result, "Lambda runtime failure"
            return json.loads(path.read_text())

    for path in ("/api/devices", "/api/releases", "/api/deployments", "/api/deployment-arms",
                 "/api/deployment-arms/00000000-0000-4000-8000-000000000000"):
        for method in ("GET", "POST"):
            request = urllib.request.Request(outputs["ApiUrl"] + path, method="OPTIONS", headers={
                "Origin": origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "authorization,content-type",
            })
            with urllib.request.urlopen(request, timeout=20) as response:
                assert response.status in (200, 204), (path, response.status)
                assert response.headers["Access-Control-Allow-Origin"] == origin
                assert method in response.headers["Access-Control-Allow-Methods"].split(",")
                allowed_headers = {header.strip().lower() for header in
                                   response.headers["Access-Control-Allow-Headers"].split(",")}
                assert {"authorization", "content-type"} <= allowed_headers
        try:
            urllib.request.urlopen(outputs["ApiUrl"] + path, timeout=20)
        except urllib.error.HTTPError as error:
            assert error.code == 401, (path, error.code)
        else:
            raise AssertionError("Public API allowed an unauthenticated request")
    print("PASS CORS preflights succeed; data routes reject unauthenticated requests")

    request = urllib.request.Request(outputs["ApiUrl"] + "/api/devices", method="OPTIONS", headers={
        "Origin": "https://untrusted.invalid", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        assert response.headers.get("Access-Control-Allow-Origin") not in ("*", "https://untrusted.invalid")
    client = aws("cognito-idp", "describe-user-pool-client", "--user-pool-id", outputs["UserPoolId"],
                 "--client-id", outputs["ClientId"])["UserPoolClient"]
    assert client["CallbackURLs"] == [parameters["CallbackUrl"]]
    assert client["LogoutURLs"] == [parameters["LogoutUrl"]]
    assert "ClientSecret" not in client
    assert client["AllowedOAuthFlows"] == ["code"]
    assert set(client["AllowedOAuthScopes"]) == {"openid", "profile", "admin-api/access"}
    print("PASS untrusted origin denied; exact Cognito code-flow callback/logout settings")

    claims = {"token_use": "access", "iss": outputs["CognitoIssuer"],
              "client_id": outputs["ClientId"], "scope": "openid admin-api/access",
              "cognito:groups": ["admins"]}
    for path, field in (("/api/devices", "devices"), ("/api/releases", "releases"),
                        ("/api/deployments", "deployments")):
        response = invoke("HttpFunctionName", {
            "requestContext": {"http": {"method": "GET"},
                               "authorizer": {"jwt": {"claims": claims}}}, "rawPath": path})
        assert response["statusCode"] == 200, (path, response["statusCode"])
        assert isinstance(json.loads(response["body"])[field], list)
    print("PASS IAM-invoked Python 3.12 runtime and DynamoDB reads")

    # Require explicit runtime disablement; also skip during stack/runtime drift.
    # Older stacks have no parameter, but still have the runtime safety gate.
    if (deployment_state == "false" and
            parameters.get("FirmwareDeploymentEnabled", "false") == "false"):
        for path in ("/api/deployment-arms", "/api/deployments"):
            response = invoke("HttpFunctionName", {
                "requestContext": {"http": {"method": "POST"},
                                   "authorizer": {"jwt": {"claims": claims}}},
                "rawPath": path, "body": "{}"})
            assert response["statusCode"] == 403
            assert json.loads(response["body"]) == {"error": "deployment_disabled"}
        print("PASS firmware arming and deployment disabled before effects")
    else:
        print("SKIP arm/deployment POST: deployment is enabled or disablement is unconfirmed")

    assert invoke("IngestFunctionName", {"ingest_type": "invalid"}) == {
        "ok": False, "error": "event_rejected"}
    print("PASS ingestion rejects invalid events")

    with urllib.request.urlopen(outputs["CognitoIssuer"] + "/.well-known/openid-configuration",
                                timeout=20) as response:
        discovery = json.load(response)
    assert discovery["issuer"] == outputs["CognitoIssuer"]
    key = aws("kms", "get-public-key", "--key-id", outputs["SigningKeyArn"])
    assert key["KeySpec"] == "RSA_2048" and key["KeyUsage"] == "SIGN_VERIFY"
    print("PASS Cognito discovery and KMS signing-key provisioning")
    print("Live checks complete; browser login, upload/signing and real telemetry are separate checks.")


if __name__ == "__main__":
    main()