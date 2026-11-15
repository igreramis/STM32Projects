# Admin frontend hosting

## Deployed configuration — 2026-11-16

Account **844274146091**, region **ap-southeast-2**, operator profile `ota-admin`.
The frontend is separate from `stm32-admin-backend` and has no firmware/API IAM
permissions. Static files are publicly viewable through CloudFront; data methods
remain protected by the backend JWT authorizer and admin checks.

| Setting | Deployed value |
| --- | --- |
| Frontend stack | `stm32-admin-frontend` |
| UI URL | https://dw4cy96r64rff.cloudfront.net/ |
| Assets bucket | `stm32-admin-frontend-assetsbucket-5vbhh8lktsrc` |
| Distribution | `E357Y9JALJ966S` |
| Backend `WebOrigin` | `https://dw4cy96r64rff.cloudfront.net` — **no trailing slash** |
| Backend `CallbackUrl` / `LogoutUrl` | `https://dw4cy96r64rff.cloudfront.net/` — **with trailing slash** |

Localhost live authentication is no longer registered. Local **?demo=1** still
works without configuration/authentication/API calls; static assets still load.
OAuth scopes are exactly **`openid profile admin-api/access`**, not `email`.
Passwords are entered only by the user in Cognito, never captured in chat/scripts.

## Infrastructure and security

[cloud-admin/frontend.yaml](../cloud-admin/frontend.yaml) provisions:

- Private S3 with all public-access blocks, bucket-owner-enforced ownership,
  versioning and **AES256** default encryption. Bucket retention applies on both
  deletion and replacement; there is no lifecycle deletion of old versions.
- CloudFront OAC with always-on SigV4 signing to the regional S3 REST origin,
  not a public S3 website. The read policy is scoped to this distribution ARN;
  insecure S3 transport is denied. Anonymous direct S3 reads remain forbidden.
- Default CloudFront TLS certificate, HTTP → HTTPS redirect, GET/HEAD only,
  HTTP/2 and HTTP/3, compression, and `PriceClass_100`.
- Zero-TTL default caching; the publisher marks runtime config, entry HTML and
  favicon **no-store**. Only hashed JS/CSS under `/assets/*` receive
  `public,max-age=31536000,immutable` (asset policy maximum one year).
- CSP: `default-src 'none'`, self-only scripts/images/fonts, self styles plus
  `unsafe-inline` for React progress styles, no objects/base URI/framing/forms,
  and upgrade-insecure-requests. `connect-src` permits only self and these exact
  API/Cognito origins, supplied as `ApiOrigin` and `CognitoOrigin`:
  - https://ujporks393.execute-api.ap-southeast-2.amazonaws.com
  - https://admin-api-844274146091-ap-southeast-2.auth.ap-southeast-2.amazoncognito.com
  No wildcard AWS, IoT, analytics or external font endpoints are needed.
- HSTS (one year), nosniff, frame DENY, no-referrer, and denied camera,
  microphone, geolocation, payment and USB permissions.

The **root callback only** is supported by this deployment: default root object
serves the entry HTML, and navigation is in memory. There is **no SPA error
rewrite**. Missing config/assets remain 403/404, never HTML with a success status;
403/404 error cache TTLs are set to zero.

CloudFront access logging is not configured; cache policies neither include nor
forward query strings, cookies or viewer headers. Do not enable access/realtime
logs, analytics or other URL/query capture that would persist OAuth callback
codes/state. The UI cleans callback query parameters after processing and does
not log tokens/raw errors. Backend API access logging is also intentionally absent.

## Publishing and backend origin changes

[cloud-admin/publish_frontend.py](../cloud-admin/publish_frontend.py) is an
explicit operator publishing tool, not an automatic build/deployment hook. It
uses the shared AWS CLI wrapper with profile/region above, checks the account and
completed stacks, checks cloud firmware deployment remains disabled, and checks
the distribution domain. Build/test/lint the UI separately before publishing.

The publisher allowlists hashed JS/CSS, favicon and entry HTML from the UI build;
it excludes source maps, example config and local runtime config. It generates
the five public runtime fields from backend/frontend stack outputs (no secrets).
It uploads **hashed assets first, config before entry HTML, and entry HTML last**,
with explicit content types, cache controls and AES256 encryption. There is
**no delete/sync-delete**: old assets stay available for open tabs and rollback.
This ordering reduces broken references but is not an atomic multi-object publish.

It invalidates `/`, `/index.html`, `/config.json` and `/favicon.svg`, and writes a
SHA-256 manifest/runtime/stack/invalidation record to
[build/cloud-admin/frontend-publish.json](../build/cloud-admin/frontend-publish.json).
Archive each known-good record before the next publish overwrites it.

For any future backend origin update, inspect current stack parameters and preserve
**`ArtifactBucket`, `ArtifactKey` and all other unchanged values** (use previous
values in a change set, or explicitly carry them forward). Change only the exact
`WebOrigin`, `CallbackUrl` and `LogoutUrl` values as intended; never revert code
by using template defaults or an older artifact. The recorded final backend key is
`admin-artifacts/backend-50dc0bf2b11fb7a2ebfac5919458c7bba59a6dabdab45953572099e126204c6d.zip`
in `iot-lab-firmware-844274146091`. Review the change set, retain deployment
disablement, and align frontend CSP parameters/runtime configuration with the
actual API/Cognito endpoints. The publisher does not update backend parameters.

## Smoke checks and recorded browser evidence

[cloud-admin/smoke_frontend.py](../cloud-admin/smoke_frontend.py) is read-only:
it compares hosted bytes/hashes against the publish manifest, validates runtime
config, security/cache headers, root serving, missing-file failures, HTTPS redirect,
private direct S3 access, public-access blocks and deployed OAC.
[cloud-admin/smoke_test.py](../cloud-admin/smoke_test.py) separately reads the
backend's current `WebOrigin` for CORS checks and verifies exact Cognito settings,
unauthenticated GET rejection, IAM-invoked runtime reads and disabled deployment.
Its simulated authorizer context is not proof of browser login.

Final validation: hosting/backend smoke passed; authenticated hosted GETs for
**devices, releases and deployments all
returned 200**; logout → Cognito login → dashboard was observed with the user
handling the password. Both gateway and STM32 were online, firmware **0.3.0**,
with **0.4.0 approved**. After the source-clock fix, fresh hosted observations
advanced every **10 seconds** (distinct from the UI's five-second polling delay).

The final local suites passed (93 backend/infrastructure and 47 frontend tests),
along with frontend build/lint and hosted-file integrity checks. No new upload was
performed at the HTTPS origin: earlier UI upload/approval was verified and exact
S3/signature/tamper checks repeated. No actual firmware deployment, install command
or hardware/button operation was performed. Both cloud and local installation
remain false; neither Lambda role has `iot:Publish`. See the
[deployment record](ADMIN-BACKEND-DEPLOYMENT.md) for tests, clock bounds and limits.

## Costs, retained versions and rollback

CloudFront requests/transfer/invalidations and S3 requests/storage can incur
charges; free-tier eligibility is not assumed. Versioning and no-delete publishing
retain old assets **and noncurrent versions**, so storage grows. Stack deletion
does not delete the retained bucket; replacement can leave another chargeable
bucket. Backend table/PITR/KMS/log costs are separate. Do not add lifecycle cleanup
or delete retained resources without an explicit retention/rollback review.

For an authorized rollback, select an archived **known-good publish manifest**,
not an arbitrary old entry HTML. Identify retained S3 versions for every object
in that manifest and verify their bytes against its SHA-256 hashes; the manifest
does not record S3 VersionIds. Preserve/recover its hashed assets first, restore
compatible config/favicon with no-store metadata, then restore its entry HTML
**last**, also no-store. Copying an old version to the current key creates a new
version; do not delete newer or older assets. Invalidate the four mutable paths
above and smoke-check against the chosen manifest, then verify browser login.
If config points to a previous origin/client, review backend CORS/Cognito and CSP
compatibility separately while preserving the backend artifact parameters.
No rollback was performed by this documentation update.