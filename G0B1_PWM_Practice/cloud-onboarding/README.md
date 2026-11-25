# Isolated device onboarding — local phase one

Status: **implemented and tested locally, not deployed**, 2026-11-25.
This is the first part of the [onboarding proposal](../docs/DEVICE-ONBOARDING-PLAN.md),
not a replacement for the working Pi/STM32 system.

## What is included

- [template.yaml](template.yaml): a separate CloudFormation stack containing an
  encrypted, retained DynamoDB table; Python registration Lambda; factory-CA event
  rule; bootstrap MQTT policy; restricted roles; failure queue and two alarms.
- [registration.py](registration.py): identity validation, atomic import/approval
  primitives, certificate checks, registration checkpoints and collision checks.
- [aws_adapter.py](aws_adapter.py): consistent DynamoDB reads and conditional
  transactions, IoT calls and the Lambda entry point. Importing modules does not
  create clients, contact AWS or read credentials.
- Three offline test modules. All generated test keys/certificates stay in memory.

Only `onboard-test-01` and `onboard-test-02` can be admitted. The worker explicitly
rejects the live Pi certificate; its IAM role denies access to that certificate
and `pi-lab-01`. Existing backend, gateway, UI, firmware and service files are not
modified. There are no OTA, UART, GPIO, firmware-bucket or firmware-signing calls.

`RegistrationEnabled` defaults to `false`: the rule is disabled, the handler exits
without making clients, and the worker has no IoT mutation permissions. Enabling
this flag does not register a CA or enable its automatic registration.

## Factory inventory and approval

1. A controlled factory station creates a unique device key pair and signs the
   device certificate with the issuing CA's private key. Alternatively, the device
   creates its own key and sends a CSR to that station. Neither option gives the
   device the CA private key.
2. `inventory_records()` validates identifiers and prepares four records:
   `inventory`, `factory_cert`, `unique_mac`, and `unique_thing`.
3. A trusted admin caller uses `import_inventory()` to write all four plus an audit
   entry in one conditional transaction. The initial state is `IMPORTED`, not
   approved. Identical imports do not reset approval; collisions do not overwrite.
4. A separate trusted decision calls `set_approval()`. The registration worker
   cannot approve inventory: it has no write permission for those partitions.

These are internal Python primitives, **not deployed admin endpoints or a factory
upload CLI**. An authenticated importer/API and its separate role still need to be
implemented and reviewed before live inventory is loaded. Always create bundles
through the validator. Actor strings must identify the authenticated operator,
not come from an untrusted request. Direct console writes are not a supported
administration path.

Records use `pk`, `sk`, canonical JSON `data`, and numeric `revision`, matching the
existing project's envelope pattern but stored in a different table. Approval
binds the SHA-256 hash of the DER certificate and the issuing CA to manufacturer,
hardware serial, model, normalized permanent MAC and allocated Thing name.
The hash of the PEM text file is **not** the certificate fingerprint.

Required leaf certificate profile:

| Field | Expected value |
| --- | --- |
| Subject CN | allocated test Thing name |
| Subject serialNumber | hardware serial, not X.509 certificate serial |
| Subject OU | approved model |
| SAN | exactly one URI: `urn:factory:mac:<12 lowercase hex digits>` |
| Basic constraints | not a CA |
| Key usage | digital signature |
| Extended key usage | TLS client authentication |

The leaf and issuing CA must be within their validity dates. The CA must have CA
basic constraints and certificate-signing key usage. The leaf must be directly
signed by that exact configured CA. Supported keys are RSA >=2048 and NIST
P-256/P-384/P-521; signatures use SHA-256/384/512 with RSA PKCS#1 v1.5 or ECDSA.
Other chains/algorithms need explicit support and tests, not implicit acceptance.

This binding does not prove the physical hardware serial remotely. Copying a
device certificate **and its private key** defeats software-only device identity.

## Registration and recovery behavior

AWS creates the initial `PENDING_ACTIVATION` certificate and emits the JITR event.
The worker checks account, configured CA, authoritative certificate status, exact
approved fingerprint, signature, signed identity fields and uniqueness records
**before** claiming any registration work.

It then persists the following checkpoints, each with an atomic audit entry:

`VALIDATING → ENSURING_THING → ATTACHING → ACTIVATING → BOOTSTRAP_ACTIVE`

It creates/verifies a Thing with exact ownership attributes, refuses unrelated
principals/policies/group membership, attaches the certificate exclusively and
adds only the bootstrap policy. Attachments are read back. Approval and current
certificate status are checked again before activation; activation is last.
The device must retry its connection after activation.

**Deliberate phase-one limit:** once a registration claim exists, another worker
cannot take it over. There is no expiring lease. Successful duplicate events return
the recorded completion without performing IoT calls (the return is a historical
checkpoint, not proof that the certificate is currently active). Pending duplicate
events do not rerun mutations. A conditional claim prevents concurrent events from
both activating. Approval decisions freeze when the claim is taken; the approval
transaction and claim transaction cannot both win a withdrawal/admission race.

An error after claiming leaves `RECONCILIATION_REQUIRED`, or the last durable
checkpoint if even recording the failure fails. A lost activation response can
mean the certificate is already active. **A recovery hold is not deactivation.**
SDK retries are disabled; Lambda's async retries are bounded and cannot take over
the claim. Exhausted failures go to the failure queue. Rule-delivery errors also
go to that queue. Both queue depth and Lambda errors have CloudWatch alarms;
notification subscriptions are not configured yet.

On a recovery hold:

1. Stop the isolated test client and stop new admission. Do not affect the live Pi.
2. Ensure the original worker has finished and queued work cannot mutate state.
3. Inspect the exact registration checkpoint/audit, certificate status, Thing
   ownership, principal/policy attachments and approval binding.
4. Review a targeted recovery action with the operator. Preserve existing records.

**Never delete a claim or blindly replay a queued event to recover.** Automatic
reconciliation and post-claim approval withdrawal are not implemented in this
phase. There is no deactivation/revocation/cleanup executor. Direct administrative
changes during registration are prohibited; IoT and DynamoDB cannot form one
atomic transaction.

Rejected unknown/unapproved inputs are logged using fixed reason codes and do not
create arbitrary identity records. Known admission transitions append audit rows.
Audit rows are ordinary DynamoDB data, not immutable/WORM storage. Lambda logs do
not include private keys, PEM certificates, raw events or SDK error messages.

## Permission boundaries

- Bootstrap allows Connect with client ID equal to the attached test Thing name,
  Publish to its own onboarding request, and Subscribe/Receive on its own response.
  No operational telemetry, commands, confirmation topic or wildcard subscriptions.
  The response/rotation handler is not present yet, so bootstrap requests do not
  currently produce certificate replacements.
- The registration worker reads only the new table. It can write registration and
  audit partitions, not inventory or approval partitions. Conditional transactions
  use `dynamodb:ConditionCheckItem` plus per-item read/write permissions.
- `AttachThingPrincipal` is restricted through `iot:thingArn` to the two test Things.
- AWS `AttachPolicy` checks the certificate target, not a policy-name ARN.
  Policy selection comes only from the stack-provided bootstrap policy setting.
  Deployment/configuration writers remain trusted; the worker cannot create/edit
  policies. `UpdateCertificate` needs account/region certificate ARNs, with the
  live certificate explicitly denied. CA/fingerprint admission is enforced in code.
- `ListAttachedPolicies` does **not** support resource-level IAM. Its read-only
  statement therefore requires `Resource: "*"` and an exact requested-region
  condition. Other certificate reads remain resource-scoped. Do not claim that
  this read is IAM-filtered by CA or certificate.
- Lambda invocation and the rule's error role are restricted to the exact rule
  and source account. A validated explicit rule name avoids a dependency cycle.

Sources checked: [IoT authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awsiot.html)
and [Thing policy variables](https://docs.aws.amazon.com/iot/latest/developerguide/thing-policy-variables.html).

## Local validation

Run the standard unittest discovery on this directory after installing
[requirements-test.txt](requirements-test.txt) into an isolated local environment.
Run `cfn-lint` against [template.yaml](template.yaml). Do not combine this directory
and cloud-admin on one Python import path: they are separate Lambda artifacts
with separate modules named `aws_adapter`.

Observed 2026-11-25:

- **43 onboarding tests pass**, including RSA/ECC, two distinct identities, invalid
  signatures/profile/validity, approval races, identity collisions, duplicate events,
  before/after failures at every IoT call and transaction checkpoint, SDK request
  shapes, role restrictions, template references/cycles and topic boundaries.
- **122 existing cloud-admin tests pass** in a separate process.
- **CloudFormation lint passes** with cfn-lint 0.87.11.
- Host interpreter: Python 3.8.10. Isolated test dependencies under ignored build
  output: cryptography 46.0.7, Boto3/botocore 1.37.38, PyYAML 6.0.3.
- No AWS API calls or MQTT connections were used by these tests. Policy tests
  inspect the template; they do not prove effective AWS authorization or broker
  behavior. Fault tests use an in-memory store; live DynamoDB is not validated.

The host is an end-of-life Python test environment, not the production runtime.
The stack targets **Python 3.12 / arm64**. Dependencies must be resolved, pinned,
security-reviewed and built for that runtime, with both Python modules and their
dependencies at the zip root. Do not package the host's x86_64 native libraries.
The artifact must be tested in that runtime and uploaded to a separately approved
private artifact bucket with an exact S3 object version. No artifact was built or
uploaded in this phase.

## Remaining gates

Before deploying/enabling even the test stack:

1. Review this implementation, permanent-hold recovery choice and IAM limits.
2. Complete the trusted importer and guarded operator recovery tooling.
3. Build and validate the Python 3.12 arm64 artifact and pin its dependencies.
4. Obtain explicit approval for test CA creation/registration, AWS resources and
   isolated test identities. Inspect the proposed CloudFormation change set.
5. Register only the test CA's public certificate, initially without automatic
   registration. Deploy the stack disabled; verify invocation permissions and
   error handling before deliberately enabling the rule/worker and CA registration.
6. Load/approve test inventory and run isolated MQTT clients with SNI, retry/backoff
   and matching Thing/client IDs. Verify both success and cross-device denial,
   including wrong client ID, absent attachment and wildcard filters. Use simulation
   only against the live namespace; never publish live commands.

Production credential issuance/rotation, automatic reconciliation, admin HTTP/UI
integration, and live Pi migration remain separate work. No commit or push has
been performed for this implementation.