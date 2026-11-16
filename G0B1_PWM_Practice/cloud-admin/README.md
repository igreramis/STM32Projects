# Backend infrastructure (default-disabled emergency gate)

Current live configuration uses mandatory single-use arms with emergency master
gates enabled. See [rollout evidence](../docs/ADMIN-SINGLE-USE-ARM-DEPLOYMENT.md).
The default for a new stack remains disabled; there is no automatic deployment.

[template.yaml](template.yaml) provisions only the admin backend in account
**844274146091**, Sydney (**ap-southeast-2**). It does not create frontend hosting,
replace the firmware bucket, configure the Pi, or perform any hardware operation.
Creating/updating the stack incurs AWS charges. No AWS operation is performed by
the offline tests.

Separate frontend infrastructure and publishing are described in
[ADMIN-FRONTEND-HOSTING](../docs/ADMIN-FRONTEND-HOSTING.md).

## Safety boundary

**Firmware deployment defaults off.** `FirmwareDeploymentEnabled=false` omits
HTTP `iot:Publish` permission and sets `FIRMWARE_DEPLOYMENT_ENABLED=false`.
Only the exact runtime string `true` enables arm/deploy POSTs; unset, malformed,
or differently cased values fail closed with `403 deployment_disabled` before
effects. The parameter's explicit `true` condition grants publishing to the exact
gateway command topic, but does not bypass the single-use arm requirement.
Ingestion never has IoT/S3/KMS permissions. Enablement requires reviewed gateway
trust/configuration and explicit operator approval, not extra bypass policies.

HTTP and ingestion use separate functions, separate least-privilege roles, and
the same `backend.handler` entrypoint. Only API Gateway can invoke the HTTP
function through the provisioned resource permissions. Only the two exact IoT
rule ARNs, constrained to the source account, can invoke ingestion. No function
URLs or unauthenticated data routes are created. The explicit OPTIONS preflight
route is unauthenticated. Account administrators with
independent IAM invocation rights remain trusted; these permissions do not revoke
their existing access.

Data methods on `ANY /api/{proxy+}` use the JWT authorizer and require
`admin-api/access`. The backend additionally checks access-token issuer/client,
scope, and `admins` membership. The more specific `OPTIONS /api/{proxy+}` route
has authorization `NONE` and an explicit Lambda integration. The handler accepts
OPTIONS before authentication and returns an empty, side-effect-free **204**;
API Gateway applies the configured CORS origin/method/header policy. Preflight
is not a no-Lambda route, and CORS is not an authorization mechanism.

The IoT rules project only the fields consumed by the backend, never `SELECT *`.
They generate `ingest_type`, `ingest_topic`, `ingest_client_id`, and
`ingest_received_at` themselves using SQL 2016-03-23 and the broker's
`topic()`, `clientid()`, and `timestamp()`. MQTT payloads cannot inject HTTP request
context or replace the trusted ingestion envelope. Ingest has only table/log
rights, even though the shared adapter initializes the S3/KMS/IoT clients.
The existing IoT certificate policy must already bind client ID `pi-lab-01` and
the expected topics to the authorized gateway; these rules do not create or
modify that policy. Initial rule creation may precede its Lambda permission;
messages in that short provisioning window are not guaranteed to be delivered.

## Single-use deployment arms

All data routes require the same administrator access token. Both POSTs accept
exactly `device_id`, `release_id`, and canonical UUID `request_id`; the deployment
must reuse its arm's UUID. Client-provided lifetimes, predecessor versions,
envelopes, or extra fields are rejected.

- `POST /api/deployment-arms`: validates approved release, fresh gateway/UART
  observations, known predecessor firmware, different target version, and no
  active lease. Atomically creates an immutable arm and checks the observation,
  release, absent job and absent lease. No KMS, S3, MQTT, job creation, lease
  creation, download or programming occurs. Multiple reviewed UUIDs can coexist;
  the gateway lease still admits only one deployment at a time.
- `GET /api/deployment-arms/{request_id}`: read-only, even while disabled.
  Returns `request_id`, `device_id`, `release_id`, `from_version`, UTC `armed_at`,
  UTC `expires_at`, and `status` (`armed`, `expired`, `consumed`). Expiry is
  calculated without a write; consumed takes precedence over expired. Unknown
  IDs return `404 arm_not_found`.
- An arm lasts exactly 120 seconds from its integer-second creation timestamp;
  the deadline is exclusive. Repeating the same arm POST returns its original
  state without extending it; rebinding the UUID is rejected. Expired/consumed
  rows are permanent tombstones, not DynamoDB TTL records. Legacy job IDs cannot
  become new arms.
- `POST /api/deployments`: requires that stored arm, unchanged original
  `from_version`, fresh observations and the exact approved release snapshot
  (including object version, signed envelope and revision). New observations
  with the same predecessor are allowed. Changed release/predecessor, stale
  observations, expiry or missing arm do not consume it or publish anything.
  The transaction consumes the arm and creates the pessimistic job plus gateway
  lease together, condition-checking the observation and release. Conflicts have
  no partial writes. Existing matching jobs reconcile without signing,
  presigning, consuming again or republishing, even after expiry.

The stored arm pins the release envelope and SHA-256 of its canonical JSON.
After consumption, KMS signs a separate `deployment_arm` envelope using exactly
the release envelope format and pinned RSA-2048 PKCS#1 v1.5 SHA-256 key. Its
payload is exactly `schema=1`, `purpose=deploy_firmware`, `command_id`,
`gateway_id`, `device_id`, `version`, `from_version`, `sha256`, `release_sha256`,
`issued_at`, `arm_expires_at`, `expires_at`. `issued_at` is the original arm
creation time, **not dispatch time**; deadlines are `issued_at+120` and
`issued_at+900`. This preserves genuine admission lifetime with over 780 seconds
of command lifetime remaining at admission. Slow preparation cannot extend the
arm: the backend rechecks expiry before publishing, and the gateway enforces
the signed deadline after verification. No API response exposes envelopes,
S3 URLs, object identifiers, or signatures.

Preparation/signing failure burns the arm, records a terminal job and releases
the lease without publishing. An uncertain publish retains the pessimistic
job/lease for investigation or a matching terminal ACK; it is never retried.
Expiry during a committed transaction/preparation also burns the arm but cannot
start MQTT dispatch after the final expiry check. As with any distributed
deadline, in-flight MQTT delivery may arrive late; the gateway journals the
authentic `deployment_arm_expired` rejection and the backend ingests it to
release the matching lease. Transient unjournaled gateway errors with no
`event_seq` remain rejected, not treated as proof of terminal completion.

[test_deployment_arms.py](test_deployment_arms.py) tests the API, no-effect
boundaries, races, signature payload and a real RSA/SDK-presigned cloud command
through the unchanged gateway verifier and durable OTA journal. Network and
hardware remain forbidden; only the installer is mocked. Gateway tests are not
modified. [smoke_test.py](smoke_test.py) checks public auth/CORS for the new routes;
it only attempts empty arm/deploy POSTs when runtime and stack disablement are
confirmed. Do not run live smoke checks as part of offline validation.

## Prerequisites (operator checks; not performed here)

- Confirm the intended account and Sydney region before any cloud operation.
- Existing bucket **iot-lab-firmware-844274146091** must be private, block public
  access, and have versioning **Enabled**, not suspended. Retain signed object
  versions; lifecycle deletion would invalidate approved releases. This stack
  neither manages nor inspects the bucket or its policy.
- The object scope is exactly `firmware/stm32g0b1/*`. HTTP may only `PutObject`
  and `GetObjectVersion` there: no bucket listing, unversioned reads, ACL changes,
  or deletion. Existing bucket policy explicit denies still apply. The role
  assumes firmware uses S3-managed encryption; existing SSE-KMS would require
  a separately reviewed data-key/decrypt policy, not the signing key.
- Existing IoT data endpoint:
  **an0l72m95csqi-ats.iot.ap-southeast-2.amazonaws.com**. This is a hostname,
  not a URL. The adapter supplies HTTPS. No endpoint discovery permission is needed.
- An existing private artifact bucket in **ap-southeast-2**. The deployment
  identity must be able to read the zip (and decrypt it if necessary), create
  the declared resources, and pass the execution roles. Artifact bucket creation
  and deployment-identity permissions are outside this template.
- Cognito domain `admin-api-844274146091-ap-southeast-2` must be available. The
  prefix includes account and region; this intentionally supports one such stack
  per account/region, not parallel copies with the same domain.

## Package and deploy (instructions only)

1. Review/test the current [backend.py](backend.py) and
   [aws_adapter.py](aws_adapter.py) together. Package those two modules at the
   **root** of a zip, not under an enclosing directory. Do not include tests,
   credentials, local configuration, caches, or virtual environments. Both
   functions run Python **3.12**, arm64, and use `backend.handler`.
2. The minimal artifact uses the runtime-provided boto3/botocore. Before release,
   validate in Python 3.12/Lambda that the S3 SDK supports `PutObject.IfNoneMatch`.
   For reproducibility, package compatible pinned boto3/botocore dependencies
   with the modules after an independent dependency review; native dependencies,
   if added, must support Linux arm64. No packages are installed by this task.
3. Upload the reviewed zip to an immutable, unique S3 key in the artifact bucket.
   Set `ArtifactBucket` and `ArtifactKey`. Use a **new key for every code update**:
   overwriting the same object does not make CloudFormation update Lambda code.
4. Validate the template and create a CloudFormation change set in Sydney using
   IAM capability acknowledgement **CAPABILITY_IAM**. Inspect the change set
   before executing it. Execution is an explicit operator step, not part of this
   implementation. There is no deployment script that runs automatically.
5. Set browser parameters consistently:

   | Parameter | Default | Requirement |
   | --- | --- | --- |
   | `WebOrigin` | `http://localhost:5173` | Origin only, no trailing slash |
   | `CallbackUrl` | `http://localhost:5173/` | Exact configured OIDC redirect URI |
   | `LogoutUrl` | `http://localhost:5173/` | Exact permitted logout URI |
   | `FirmwareBucket` | Existing lab bucket | Fixed to this lab |
   | `IotDataEndpoint` | Existing Sydney hostname | Fixed to this lab |

   Production origins/redirects must use HTTPS. Callback/logout URL origin and
   path must match the separately configured browser app; CloudFormation does
   not enforce cross-parameter equality. No browser hosting is provisioned here.
  The defaults above are not the live settings: deployed `WebOrigin` is
  `https://dw4cy96r64rff.cloudfront.net` (no trailing slash); `CallbackUrl` and
  `LogoutUrl` are `https://dw4cy96r64rff.cloudfront.net/`. Localhost live login
  is no longer registered. When updating these parameters, preserve the current
  `ArtifactBucket`/`ArtifactKey` and all other unchanged stack parameters; do not
  reset them to defaults or redeploy an older artifact.
6. After stack completion, separately create the admin user and add it to the
   `admins` group using `UserPoolId`. Self-signup is disabled. Set any password
   directly in a trusted terminal/console, never in chat, stack parameters,
   source files, or shell history. No users or passwords are embedded here.
7. Configure the browser with output `ApiUrl` as its API base URL,
   `CognitoDomain` as the hosted-login base URL, `ClientId`, Sydney region, and
   the chosen callback URI. Use authorization code with **S256 PKCE**, requesting
   `openid profile admin-api/access`; send the **access token**, not the ID token.
   Cognito's public client has no secret and only the code OAuth flow. Cognito
   has no CloudFormation switch making PKCE mandatory: the browser must supply
   the challenge/verifier. No claim of server-side PKCE enforcement is made.
8. Read-only/auth checks and upload/approval testing may be planned separately.
   Do not call the deployment endpoint. Exporting the KMS public key and pinning
   it plus `SigningKeyArn` on the gateway is a later authorized operation; the
   Lambda role cannot export or manage keys, and this task reads no secrets.

## Resources and lifecycle

- DynamoDB: string `pk`/`sk`, on-demand billing, encryption, PITR. IAM grants the
  actual `GetItem`, `Query` (HTTP only), `PutItem`, `DeleteItem`, and
  `ConditionCheckItem` actions. `TransactWriteItems` authorization uses underlying
  item actions; there is no fictitious transaction IAM action in the role.
- Signing key: RSA_2048, SIGN_VERIFY. HTTP can only `kms:Sign` on this key with
  `RSASSA_PKCS1_V1_5_SHA_256`. The account-root key-policy statement delegates to
  IAM without referencing the Lambda role, avoiding a key/role dependency cycle.
  Asymmetric keys do not use automatic symmetric-key rotation.
- Table and signing key both retain on stack deletion **and replacement**.
  Retained resources may continue to incur charges. Restoring/importing a table,
  adopting an old retained key, and eventual deletion require explicit operator
  planning. Recreating the stack otherwise creates new resources and key trust.
- Two pre-created CloudWatch log groups retain logs for **14 days**. Roles can
  only create streams/write events in their own group, not create arbitrary
  groups. Logs are not retained on stack deletion. API access logging and raw
  MQTT logging are intentionally absent; do not add token, URL, or payload logs.
- Cognito client explicitly depends on the resource server so the custom scope
  exists first. IoT permissions depend on rule ARNs, not the reverse. Log groups
  do not depend on functions. Offline tests check the complete dependency graph.

Outputs: `ApiUrl`, `CognitoIssuer`, `ClientId`, `CognitoDomain`, `SigningKeyArn`,
`TableName`, `HttpFunctionName`, `IngestFunctionName`, and `UserPoolId`.

## Offline validation and remaining gaps

[test_infrastructure.py](test_infrastructure.py) uses stdlib `unittest` and an
existing PyYAML installation to parse the template's long-form intrinsic
functions. Run unittest discovery in this directory with pattern
`test_infrastructure.py`. If PyYAML is absent, the template suite explicitly
**skips**; it does not install anything or claim successful template validation.
The tests check resource/IAM/auth boundaries, MQTT projections, handler wiring,
retention, parameters, output references, and dependency cycles without importing
the application or contacting AWS.

These are structural regression tests, not AWS service validation. The separate
[deployment record](../docs/ADMIN-BACKEND-DEPLOYMENT.md) records completed live
backend, browser, telemetry and signing checks; do not confuse offline tests with
that evidence. No checks or cloud operations were rerun for this documentation
update. Firmware installation and operating-state/button verification remain
deferred pending explicit approval; no actual firmware deployment is established.