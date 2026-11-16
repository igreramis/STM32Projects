# Relay / Edge Operations

React + TypeScript + Vite admin console for gateway/subdevice observations,
firmware upload/approval, and deployment history. No AWS SDK, embedded credentials,
external fonts, analytics, automatic deployment, or automatic rollback.

**Live UI:** https://dw4cy96r64rff.cloudfront.net/
See [hosting](../docs/ADMIN-FRONTEND-HOSTING.md) and the
[deployment record](../docs/ADMIN-BACKEND-DEPLOYMENT.md) for operational evidence.
This frontend does not alter cloud or gateway installation configuration.

**Current rollout:** [single-use authorization deployment](../docs/ADMIN-SINGLE-USE-ARM-DEPLOYMENT.md).
The new UI/backend/Pi policy is live: 79 UI tests/build/lint passed; authenticated
browser telemetry reports 0.4.0 idle. No new firmware install was initiated by
this rollout. The historical validation notes below predate it.

## Local development and demo

Use Node 22.12+; this workstation's Node is at
`/home/wh/.local/share/ota-node/bin` (prepend it to PATH for npm/node).
Check for another running npm installation before installing dependencies.
Scripts: `npm run dev`, `npm run build`, `npm run lint`, `npm test`,
`npm run test:watch`, and `npm run preview`.

Open the Vite URL with **?demo=1** for an explicitly labeled demo. It selects
in-memory fixtures before runtime configuration, authentication, or API fetches;
uploads, approvals, deployment progress, hashes and verification flags are
simulated. Reload resets fixtures. Loading static application assets still
requires the host. Missing live configuration never falls back to demo.

**Localhost live authentication is no longer registered in the deployed Cognito
client.** Use the hosted origin for live login; explicit local demo still works.

## Runtime configuration and authentication

The host serves a no-store JSON object at `/config.json`; see
[public/config.example.json](public/config.example.json). Local Vite reads
[public/config.json](public/config.json), but the hosted publisher generates
configuration from stack outputs rather than uploading that local file.

| Field | Meaning |
| --- | --- |
| `apiBaseUrl` | HTTPS API base, without the `/api` route segment |
| `cognitoDomain` | Full HTTPS hosted-login domain, no path |
| `userPoolClientId` | Public app client ID, no secret |
| `redirectUri` | Exact registered callback/logout URL, on the UI origin |
| `region` | `ap-southeast-2` for this deployment |

Use authorization code + S256 PKCE with **`openid profile admin-api/access`**
(not `email`). The backend enforces access-token issuer/client, scope and `admins`
membership. Only access tokens go to the API. `oidc-client-ts` stores user/state
in sessionStorage, never localStorage, and uses explicit hosted-domain OAuth
endpoints rather than hosted-domain discovery. Callback query parameters are
removed after processing; raw exceptions, URLs and tokens are not logged/displayed.

Sign-out clears the local OIDC user and redirects to Cognito logout. Expiration
locks actions until explicit sign-in; no silent refresh iframe or background login.
The deployed callback/logout is the root URL with trailing slash. The runtime
accepts HTTP only for localhost/127.0.0.1 callbacks, which does not register them
with Cognito or change deployed CORS.

## Behavior and safety

- Connectivity requires the API online flag and a nonfuture observation no older
  than 35 seconds; subdevices also require a fresh gateway. State, firmware and
  updating are separate last-known observations, not inferred installation proof.
- Sequential snapshot polling waits five seconds after completion. Requests have
  a 15-second bound and abort cleanup. Failure preserves last-known data but locks
  new deployment; errors are sanitized and recovery is explicit.
- Upload validates numeric semver, safe `.bin` names, 8-byte–20-KiB size, vectors
  and embedded version; the server remains authoritative. Upload never approves
  or deploys automatically. An uncertain upload may reserve a version: do not
  blindly retry.
- Deployment review requires the sole target, an approved release, fresh
  gateway/subdevice, reported firmware and no updating/active job. Preconditions
  are rechecked on both explicit actions: **Arm this deployment**, then separately
  **Confirm and send**. Review displays the target, current and approved versions.
  Arming lasts 120 seconds and does not publish or install firmware. The UI shows
  a live countdown, blocks expired arms, and checks current firmware still matches
  the arm. These UI gates do not override backend readiness checks.
- A UUID is persisted **before arm POST**, with a stage distinguishing arm pending,
  armed and deployment pending. Deployment POST uses the exact same intent.
  No POST is retried, automatically or manually. Uncertain results retain their
  identity across reload and same-tab reauthentication, scoped to API/client/user.
  Reload requires a read-only arm lookup before sending; legacy bare saved intents
  are treated as uncertain deployments, never as fresh arms. Corrupt/unavailable
  persistence fails closed.
- Arm uncertainty is reconciled through GET; deployment uncertainty only through
  deployment history. Missing arm/history records do not authorize a new UUID.
  Only a known unused expired arm may be explicitly abandoned, followed by a new
  review and explicit arm click with a fresh UUID. Any attempted dispatch stays
  locked until history reconciles it, even if its arm subsequently expires.
- After sending, follow physical prompts in Pi journal. No SSH actions, gateway
  toggles, or hardware button instructions are provided by this dashboard.
- `dispatch_unknown` remains blocking even with visible `error` status. Progress
  uses reported stages, not elapsed time; flash/application verification remain
  distinct and are never inferred from dispatch.
- Onboarding and Telemetry tabs are intentionally disabled, including on mobile.

## Validation and limits

### Arm API contract

- `arm(body: Intent, signal)` → `POST /api/deployment-arms`
- `armStatus(requestId, signal)` → `GET /api/deployment-arms/<uuid>`
- `Intent = { device_id: 'stm32g0b1-01', release_id: numeric semver, request_id: UUID }`
- Both arm endpoints return `Arm = { ...Intent, from_version: numeric semver,
  armed_at: ISO UTC, expires_at: ISO UTC, status: 'armed' | 'consumed' | 'expired' }`.
  Expiry is 120 seconds after arming; the same UUID never renews it.
- `POST /api/deployments` keeps its existing Intent body and requires the matching
  prior arm. API error messages are allowlisted; unknown errors remain generic.
- Demo implements the same matching, expiry, no-renewal and consumption semantics
  entirely in memory. Reload resets demo fixtures, not pending safety records;
  a missing demo arm after reload therefore remains locked, just like a missing
  live arm. No live API calls are made.

Vitest/Testing Library tests are offline; unexpected fetches reject by default.
Prior recorded evidence: **47 UI tests, build and lint passed**. Hosted authenticated
GETs for devices/releases/deployments returned 200, and logout → Cognito login →
dashboard was observed with the user entering the password. Both devices were
online, STM32 firmware 0.3.0, with 0.4.0 approved.

Prior audit: **0 runtime vulnerabilities; 7 moderate development lint-chain
findings**, involving `minimatch` 10.2.6, GHSA-g3hv-75wg-m249. Review dependency
compatibility; do not blindly force a downgrade.

These are recorded results, not tests rerun for this documentation update. No new
upload was tested at the HTTPS origin (earlier UI upload/approval was verified;
release checks were repeated). No actual firmware deployment or 0.4.0 hardware
installation/button test occurred; those require separate explicit approval.
