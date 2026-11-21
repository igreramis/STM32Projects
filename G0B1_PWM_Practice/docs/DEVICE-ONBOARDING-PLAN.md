# Cloud device onboarding — inspection and proposed integration

Status: **proposal only**, 2026-11-21. No onboarding implementation or deployment yet.
This is a new design, not evidence of an original proprietary implementation.
The separately referenced reconstructed work log was not present in the supplied
attachments or found in the workspace; this proposal follows the supplied task.

## 1. Verified baseline

Read-only AWS inspection used profile `ota-admin`, account `844274146091`, region
`ap-southeast-2`. AWS CLI authentication worked; Cognito browser authentication
was not revalidated and is a separate session.

| Item | Observed result |
| --- | --- |
| Git | Root is parent STM32Projects; master at c79ebc3, origin/master at same commit; only pre-existing editor settings modification before this document |
| Pi service | pi-iot-ota.service active/running, PID 1240; no restart performed |
| MQTT | Configured client ID pi-lab-01, supplied ATS endpoint, port 8883 |
| Credential | AWS-issued certificate, ACTIVE, no registered customer CA association |
| Certificate ID / SHA-256 | f3190d109b88f9234702070d246c4d109f83012b48748fe193165371933e10d0 |
| Thing attachment | Exactly one: pi-lab-01, NON_EXCLUSIVE_THING; reverse lookup confirms the same single principal |
| Thing groups | None |
| Policy | pi-lab-01-policy, default version 1, exact client and five existing action/resource statements; no Thing variables |
| Registered CAs | None in this account/region |
| IoT rules | Only existing telemetry and acknowledgement rules, both enabled |
| Certificate providers | None |
| DynamoDB tables | Only existing AdminTable; no separate manufacturing table |
| Backend stack | UPDATE_COMPLETE; FirmwareDeploymentEnabled=true, left unchanged |
| Application | At 00:41:08 UTC: 0.5.0, idle, updating=false; gateway observation ~5 seconds old, UART observation ~12 seconds old |

The Pi runtime uses `/home/pi/aws-iot/config.json`, the existing Python virtual
environment and explicit OTA configuration. The live certificate basename is
`device.pem.crt`, not the example configuration's certificate basename. The runtime
directory is mode 0700; certificate, private-key and server-root files are mode
0600, owned by pi. Configuration is mode 0664 inside that private directory.
Only credential paths, certificate metadata and file permissions were inspected;
private-key contents were not read or exported.

AWS TestAuthorization allowed the existing client's Connect, telemetry Publish
and command Subscribe operations. It denied another client's Connect/telemetry
Publish/command Subscribe and denied publishing to the live command topic.
These are policy simulations, not new MQTT connections or packet-level tests.

### Repository integration points

- [Gateway configuration and mTLS loading](../gateway/pi-client/client.py#L31-L66)
  restrict the working client ID to pi-lab-01; [connection creation](../gateway/pi-client/client.py#L180-L196)
  loads certificate/key paths. No credential rotation is implemented there.
- [Backend identities](../cloud-admin/backend.py#L17-L18) distinguish gateway
  pi-lab-01 from STM32 application identity stm32g0b1-01. Only the former is a
  live AWS IoT Thing. The latter is reported by the gateway, not separately
  authenticated to AWS using a device certificate.
- [Ingest validation](../cloud-admin/backend.py#L434-L492) and
  [IoT rules](../cloud-admin/template.yaml#L409-L439) are intentionally fixed to
  the existing gateway. Provisioning a test Thing must not generalize OTA admission.
- [DynamoDB adapter](../cloud-admin/aws_adapter.py#L8-L68) uses pk/sk, JSON data,
  revision and conditional transactions. Reuse this pattern, not the live records.
- [Admin authorization](../cloud-admin/backend.py#L123-L130) checks admins membership;
  [UI onboarding navigation](../admin-ui/src/App.tsx#L173) is presently disabled.
- No manufacturing allowlist, JITR handler or rotation workflow was found in the
  inspected project. The live Thing/policy exist outside the backend template;
  absence from that template does **not** mean they do not exist in AWS.

## 2. Isolation and implementation boundary

Add a separate onboarding CloudFormation stack, table, registration/rotation/admin
Lambdas, fixed IoT policies, retry/dead-letter handling and bounded reconciliation.
Place implementation alongside cloud-admin, a standalone onboarding client
alongside gateway/pi-client, and tests/documentation beside those components.

Do not replace or adopt the existing Thing, certificate, policy, AdminTable,
firmware bucket, KMS signer, OTA handlers or Pi service into the new stack.
Protect the live certificate and Thing explicitly in new mutation-role policies.
Initially accept only test Things `onboard-test-01` and `onboard-test-02` and the
new test factory CA. Test clients have no UART, GPIO or installer dependencies.

Factory CA registration is a separately approved, guarded deployment step using
only its public certificate, with account/region/fingerprint checks. The stack
consumes its ID; no private CA key in CloudFormation parameters, artifacts or Lambda.
Prepare the rule/handlers before deliberately enabling CA auto-registration.
Retain identity/audit resources on stack removal; no automatic certificate cleanup.

Later, add explicit onboarding routes/integration to the existing HTTP API using
the existing Cognito authorizer and application-level admins check. Keep the OTA
HTTP role unchanged. Enable the UI onboarding page only when that API is ready.
Keep the existing two-device dashboard/OTA contract unchanged initially; a
multi-gateway operational registry is a later, separately reviewed integration.

## 3. Manufacturing identity and DynamoDB model

Use a new OnboardingTable with the familiar pk/sk/data/revision envelope.
Top-level expiry attributes may support TTL, but TTL is never an authorization
check and must not remove identity reservations or audit history.

| pk | sk | Purpose |
| --- | --- | --- |
| inventory | manufacturer + hardware serial | Model, permanent MAC/interface inventory, batch, import provenance and approval state |
| unique_mac | normalized 12-hex MAC | Exclusive inventory owner |
| unique_thing | allocated Thing name | Exclusive inventory owner; do not derive authority from an arbitrary client ID |
| factory_cert | SHA-256 certificate fingerprint | Approved CA ID, inventory owner, signed identity fields, validity, lifecycle |
| device | allocated Thing name | Manufacturing reference, optional application/subdevice associations, current/candidate credential generation |
| registration | factory certificate ID | Step, attempt, state revision, recoverable external-operation checkpoints |
| rotation | Thing name + request UUID | Old principal, CSR/public-key hashes, nonce hash, deadline, candidate certificate and phase |
| audit:<Thing-or-inventory-ID> | transition UUID | Actor, reason code, old/new state, correlation ID and server timestamp |

Import first, approve separately. Only the manufacturing-admin handler may create
or approve inventory and fingerprint bindings; device-facing Lambdas cannot do so.
Normalize MACs, reject multicast/zero/broadcast values, record the permanent
interface role, and never use a randomized runtime MAC as the inventory identity.
Validate bounded serial/model/Thing strings; reject MQTT/IAM wildcard characters.
Manufacturing serial numbers and X.509 certificate serial numbers are distinct.

Create inventory, uniqueness reservations and audit atomically. A duplicate import
with identical content is idempotent; a changed mapping or fingerprint collision
is a conflict, never an overwrite. Approval binds the exact certificate fingerprint
and CA to the serial/MAC/model mapping, not merely a claimed serial or MAC.

Proposed factory certificate profile: CN = allocated Thing name; subject
serialNumber = hardware serial; OU = approved model; SAN URI =
`urn:factory:mac:<12-hex-permanent-MAC>`. Strictly parse single required values and
reject ambiguous duplicates. Validate against the approved manufacturing record.
Generate each device's factory key on that device where practical and sign its CSR
at the controlled factory station. Keep the factory CA private key offline/protected;
an isolated lab CA is not a production PKI custody solution.

The TLS handshake proves possession of the factory private key. The manufacturing
binding connects that credential to approved inventory. It does not remotely prove
the physical MAC, tamper resistance or authenticity of the attached STM32.

## 4. JITR registration state machine

Manufacturing: IMPORTED -> APPROVED (or REJECTED).
Onboarding: PENDING_ACTIVATION -> VALIDATING -> ATTACHING -> BOOTSTRAP_ACTIVE.
Failure states: REJECTED, RETRY_PENDING, RECONCILIATION_REQUIRED.

Use JITR, not a JITP provisioning template and not a fleet-provisioning claim.
Recommend SNI_ONLY CA registration and verified SNI use on the exact ATS hostname.
AWS currently documents that the CA need not be presented in the first TLS chain
when SNI is supplied. Validate leaf-only and leaf-plus-issuer behavior with the
actual test SDK; do not confuse the factory issuer with the server trust root.
DEFAULT CA registration instead requires its ownership-verification certificate.

The rule listens only on `$aws/events/certificates/registered/<FactoryCaId>`.
Documented payload fields are certificateId, caCertificateId, timestamp,
certificateStatus, awsAccountId and certificateRegistrationTimestamp. There is
no manufacturing serial or MAC in this event. Lambda obtains and validates the
certificate through DescribeCertificate and cross-checks the configured CA/account.

AWS registers the new certificate as PENDING_ACTIVATION. It cannot establish an
authorized MQTT session until activated; the client retries with bounded backoff
and jitter. Events can repeat on pending-certificate connection attempts.

Registration Lambda:
1. Validate event shape/account/CA, then fetch authoritative current certificate
   status and public certificate. Never reactivate retired or revoked credentials.
2. Validate chain/validity/profile, denylist and exact approved fingerprint binding.
3. Claim a revision-guarded registration operation and the reserved Thing identity.
4. Create or verify the expected Thing; reject existing ownership/attribute or
   principal collisions rather than adopting another device's Thing.
5. Attach the factory certificate to exactly that Thing and the fixed bootstrap
   policy; read back attachments. No operational policy for factory credentials.
6. Recheck approval and phase, activate last, then record BOOTSTRAP_ACTIVE and audit.

No distributed transaction spans IoT and DynamoDB. Persist intended operations,
inspect external results on recovery, and reconcile partial attachment/activation
before advancing. Serialize approval withdrawal with registration work; a stale
worker must not reactivate after a terminal decision. An uncertain in-flight call
is not resolved merely by expiring a worker lease. Use bounded async retries,
dead-letter alarms and a reconciler that handles only known onboarding records.
Rejected certificates remain without usable permissions; any later deactivation
is an explicitly approved lifecycle action, never account-wide cleanup.

## 5. Rotation protocol and recovery

Proposed issuance method: AWS IoT CreateCertificateFromCsr with setAsActive=false.
It returns only certificate data; the new private key stays on the device. This
avoids operating a production CA for the lab. The firmware-signing KMS key is
unrelated and is not reused. A private production CA would be a separate decision.

Cloud phases: CHALLENGED -> CSR_ACCEPTED -> ISSUING -> CANDIDATE_READY ->
NEW_CREDENTIAL_PROVEN -> PRODUCTION_READY -> RETIREMENT_PENDING -> COMPLETE.
Unknown external outcomes enter ISSUANCE_UNKNOWN or RECONCILIATION_REQUIRED.

Topic contract (versioned JSON, QoS 1, no retained messages):

| Topic | Device action | Allowed credential |
| --- | --- | --- |
| onboarding/v1/<ThingName>/request | Publish begin / submit CSR / query status | Approved bootstrap credential |
| onboarding/v1/<ThingName>/response | Subscribe + Receive challenge / certificate / status | Bootstrap or candidate verification credential |
| onboarding/v1/<ThingName>/confirm | Publish challenge proof / committed readiness | New candidate/production credential only |

Use request UUID, protocol version, credential generation and server-issued
single-use nonce with server deadline. Bind the operation to the authenticated old
principal, allocated Thing, CSR hash and public-key hash. Reject the old public key,
invalid CSR signatures, unsupported algorithms, changed-content duplicates and
expired/replayed requests. Do not accept client-provided response-topic routing.

IoT Rules explicitly construct an envelope separating payload from broker-derived
principal(), clientid(), topic() and timestamp(). Lambda checks the broker
certificate thumbprint against stored credentials; a payload certificate ID or
"reconnected" flag is not proof. Direct invocation is restricted to the exact
rule/account. Invalid IAM/non-X.509 principals are rejected on device routes.

Device sequence:
1. Persist request metadata and a newly generated key in a private candidate slot.
2. Obtain a challenge and submit the CSR over the factory-authenticated connection.
3. Cloud creates an INACTIVE certificate, persists its identity, attaches it to the
   reserved Thing with verification-only permissions, then activates for proof.
4. Device checks the returned public key matches its private key, persists and
   fsyncs candidate files. Disconnect the factory session before trying the same
   Thing/client ID with the candidate; never run competing reconnect loops.
5. Confirm over the candidate-authenticated connection. Cloud verifies principal
   and challenge before attaching the fixed operational policy.
6. Device durably switches its credential pointer, verifies required operational
   publish/subscription permissions, and reports committed readiness using the new
   certificate. Reconnect again using only the saved production selection.
7. Retirement becomes eligible only after these proofs and a recovery grace period.

Proposed lab recovery policy: preserve the bootstrap credential until two successful
new-credential sessions, durable-selection readiness and a 24-hour grace period
have all been established. Retirement is disabled by default and separately
approved for the test certificate. First retirement is reversible INACTIVE, not
REVOKED/deleted. No old credential deletion from the device in this phase. Missing
proof or timeout enters recovery hold, not automatic retirement. After retirement,
recovery requires audited operator intervention or the current production credential,
not a factory self-reactivation path. Future production-to-production renewal uses
a separately authorized lifecycle policy and the same generation-bound protocol.

Use 0700 credential directories, 0600 files, restrictive umask, atomic pointer
replacement, directory/file fsync and a process lock. Restart chooses a complete
persisted slot. Interrupted trials can fall back to the still-valid bootstrap slot;
no changes to the live Pi service or its present credential selection during testing.

Important: CreateCertificateFromCsr has no idempotency token; the same CSR creates
another certificate. Disable automatic SDK retries for issuance. A lost response
must not trigger blind issuance or certificate activation. Leave any unknown
certificate INACTIVE/unattached, preserve factory recovery and reconcile using
issuance evidence/public-key matching. A bounded replacement issuance requires
an explicit superseding attempt and must never activate an untracked predecessor.

## 6. Authorization and least-privilege IAM

Operational IoT policy uses `${iot:Connection.Thing.ThingName}`:
- Connect: client/<ThingName>, requiring IsAttached=true.
- Publish: topic/lab/<ThingName>/telemetry and topic/lab/<ThingName>/acks.
- Subscribe: topicfilter/lab/<ThingName>/commands.
- Receive: topic/lab/<ThingName>/commands.

Require client ID == Thing name even if new attachments use EXCLUSIVE_THING.
Existing non-exclusive attachment stays unchanged. Bootstrap/verification policies
permit only their individual onboarding topic leaves and required Connect action.
No wildcard topic subscriptions, command publishing or operational access for
factory credentials. Escape IoT variables correctly when using CloudFormation
Fn::Sub. Fixed policies are created by deployment, not authored from device input.

Separate roles:
- Manufacturing admin: new table's inventory/reservation approval transactions,
  audit writes and reads. Cognito admins required; no IoT activation/issuance.
- Registration worker: approved-inventory reads, lifecycle/audit writes, CA/cert
  inspection, test-Thing creation, certificate/Thing attachment and activation.
- Rotation worker/issuer: rotation state, CreateCertificateFromCsr, candidate
  verification/operational attachment and exact onboarding response publishing.
- Retirement executor: separately gated, only tracked eligible test credentials.
- Device clients: MQTT policies only; no AWS IAM keys, DynamoDB or IoT control plane.

IAM limitations must be explicit, not hidden behind fictional condition keys:
- CreateCertificateFromCsr requires Resource="*"; isolate this action and constrain
  aws:RequestedRegion, invocation and application admission/rate limits.
- Certificate IDs are generated hashes, not prefixable Thing names. JITR workers
  need scoped account/region cert ARNs, with an explicit deny for the live cert.
  Do not claim CA-specific IAM filtering or certificate resource tags are available
  when the service authorization table does not support them.
- AttachThingPrincipal authorizes the certificate target and supports iot:thingArn;
  use it to restrict attachments to the exact test Thing ARNs.
- AttachPolicy authorizes certificate/group targets, not a policy-name ARN allowlist.
  Policy-name selection therefore needs fixed code configuration and validation;
  remove policy creation/edit permissions. Document this remaining trust boundary.
- Scope DynamoDB rights to the new table and appropriate record partitions, logs
  to dedicated log groups, responses to test topic ARNs, and Lambda invocation to
  exact source rule ARN plus SourceAccount. No access to firmware/KMS/OTA commands.

## 7. Implementation and acceptance gates

1. Agree this integration boundary and certificate/recovery choices.
2. Implement pure state-machine logic, adapters, IaC, standalone device client and
   offline fault-injection tests. Preserve Python 3.8 compatibility for host-testable
   modules; test the client on Python 3.13 and Lambda artifacts on their chosen
   supported runtime. Do not change the live service's dependency environment.
3. Review template/IAM and change set. Register an isolated lab CA and deploy the
   separate test stack only with explicit authorization. No live certificate import.
4. Validate with two isolated test identities and separate credential/state stores.
   A test client on the host needs no STM32 or Pi UART access. Existing hardcoded
   gateway Config.load remains unchanged during these trials.
5. Add admin import/approval/status UI and protected API after backend validation.
6. Separately review any future migration of pi-lab-01. No migration is authorized
   by this plan, and onboarding does not authorize an STM32 OTA trial.

Required test evidence:
- Approved factory certificate succeeds; unknown CA/fingerprint, unapproved
  inventory, serial/MAC/model mismatch, duplicate identity and Thing collision fail.
- Replayed/concurrent JITR events converge without duplicate ownership or retired
  certificate reactivation; fail each external-operation/checkpoint boundary.
- Repeated identical CSR requests return the recorded result; altered CSR/nonce,
  forged principal, wrong Thing, old-key reuse and replay are rejected.
- Lost issuance response, lost reply/PUBACK, process interruption, partial file
  writes and simulated power loss retain a recoverable credential and durable state.
- Only the new principal can prove rotation; first TLS success alone cannot retire
  the factory certificate. Expired/unconfirmed trials do not lock out the device.
- Test Things A/B cannot connect/publish/subscribe/receive as each other, including
  wildcard filters, wrong client IDs and missing attachments. Use broker tests
  between test identities; use simulation only against the live command namespace.
- After separately approved retirement, a fresh old-cert connection fails and the
  production certificate still connects. Never revoke the live Pi credential.
- Logs expose only fixed reason codes, correlation IDs, hashes and validated
  identifiers, not raw events, keys, CSRs, nonces or tokens. Every state transition
  has a conditionally appended audit entry; audit is not claimed to be WORM storage.
- Confirm original policy/Thing/service configuration remains unchanged and fresh
  0.5.0 telemetry continues. No test imports or invokes OTA, UART or GPIO modules.

No implementation tests were run during this inspection; historical test counts
are not represented as current results. No AWS writes, MQTT messages, service
changes, credential changes, commits or pushes were performed.

## Official AWS sources checked

- [JITR: pending activation, event payload/repetition, SNI and optional CA chain](https://docs.aws.amazon.com/iot/latest/developerguide/auto-register-device-cert.html)
- [CA registration: SNI_ONLY versus DEFAULT and ownership verification](https://docs.aws.amazon.com/iot/latest/developerguide/register-CA-cert.html)
- [Thing policy variables and attachment/client-ID requirements](https://docs.aws.amazon.com/iot/latest/developerguide/thing-policy-variables.html)
- [CreateCertificateFromCsr: key requirements and non-idempotent CSR reuse](https://docs.aws.amazon.com/iot/latest/apireference/API_CreateCertificateFromCsr.html)
- [IoT SQL principal and broker context functions](https://docs.aws.amazon.com/iot/latest/developerguide/iot-sql-functions.html#iot-sql-function-principal)
- [IoT IAM action/resource/condition support](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awsiot.html)