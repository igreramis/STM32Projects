# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  # Relay / Edge Operations

  Frontend-only React + TypeScript admin console for the existing gateway and subdevice API. No AWS SDK, embedded credentials, automatic deployment, automatic rollback, external fonts, or analytics.

  ## Local development

  Use Node 22.12+ (this workspace has Node at `/home/wh/.local/share/ota-node/bin`; prepend that directory to PATH for every npm/node command). Dependencies are already installed. Before any future install, check that no other npm installation is running.

  Scripts: `npm run dev`, `npm run build`, `npm run lint`, `npm test`, `npm run test:watch`, `npm run preview`.

  Open the local Vite URL with **?demo=1** for a conspicuously labeled demo. This branch is selected before configuration loading or OIDC initialization. It uses in-memory fixtures for all reads/uploads/approvals/deployments and never invokes fetch or authentication. Demo progress is simulated; hashes and verification flags are fixtures. Reload resets demo data. Static frontend asset requests are still needed to load the application.

  Without the explicit flag, the console requires real configuration and sign-in. Missing configuration never falls back to demo.

  ## Runtime configuration and authentication

  Serve a JSON object at **/config.json** with the five fields shown in [public/config.example.json](public/config.example.json). For local Vite use, provide it as public/config.json. The example is not live configuration and no deployment-specific values are invented or bundled.

  | Field | Meaning |
  | --- | --- |
  | apiBaseUrl | HTTPS API origin/base path, without the trailing /api route segment |
  | cognitoDomain | Full HTTPS Cognito hosted domain, without a path |
  | userPoolClientId | Public app client ID, without a client secret |
  | redirectUri | Exact registered callback and logout URI on the UI origin |
  | region | AWS region, e.g. ap-southeast-2 |

  Use HTTPS for deployed UI and API. HTTP callbacks are accepted only for localhost/127.0.0.1 development. Serve configuration with no-store caching. Register the exact redirect URI as both an allowed callback and sign-out URL. The static host must serve the app at the callback path (a root callback is simplest). Allow the UI origin and Authorization/Content-Type headers in API CORS.

  Cognito must enable authorization code + PKCE, `openid`, `email`, and `admin-api/access`. The signed-in account must belong to `admins`; authorization remains server-enforced. `oidc-client-ts` manages OAuth state and tokens in sessionStorage, never localStorage. Its hosted-domain OAuth endpoints are explicitly configured because Cognito hosted domains do not provide OIDC discovery. Only access tokens are sent to the API. No raw exceptions, URLs, or token values are logged or displayed.

  Sign-out clears the local OIDC user and redirects to the Cognito logout endpoint. Token expiration locks actions and asks for sign-in; no hidden refresh iframe or background login is used. Pending deployment identities survive reauthentication within the same tab and are scoped to API/client/user.

  ## Safety and behavior

  - Connectivity requires the API's online flag plus a nonfuture observation no older than 35 seconds. A subdevice also requires its gateway to be fresh. State, firmware, and updating are independent last-known observations.
  - Sequential snapshot polling runs every five seconds after completion, with abort cleanup, 15-second bounded HTTP requests, sanitized errors and explicit recovery. Poll failure preserves last-known data but locks new deployment.
  - Upload checks strict numeric semver, safe `.bin` filenames, 8-byte–20-KiB size, vector entries and embedded version. Server validation is authoritative. Upload never approves or deploys automatically. An uncertain upload can reserve a version; the UI warns against blind retries.
  - Deployment requires the sole subdevice, a selected approved release, fresh gateway/subdevice, reported current firmware, no update/active job, and explicit confirmation. Preconditions are rechecked when confirmation is clicked.
  - A UUID is persisted in sessionStorage **before** a deployment POST. Uncertain responses preserve the exact target/release/request tuple, including across reloads. Retrying is explicit, confirmation-gated, and uses the same UUID. No automatic POST retry or ambiguous republish is attempted. History reconciles pending requests. Corrupt/unavailable persistence fails closed.
  - A `dispatch_unknown` job stays blocking even if its visible status is `error`; it requires investigation. Progress represents reported stages, not elapsed time. Flash and application verification are displayed separately and never inferred from dispatch.
  - Onboarding and Telemetry are intentionally visible and disabled, including on mobile.

  ## Tests and verification limits

  Vitest + Testing Library run entirely offline: fetch rejects by default and API tests replace it with local responses. Tests cover observation age and state separation, disabled tabs, firmware constraints, demo isolation, access-token headers, session storage, request identity across uncertain retries/reloads, confirmation gates, polling cleanup and error recovery.

  Build/lint/tests do not establish real Cognito/API integration, visual browser validation, or device installation success. Those require separate validation with actual runtime configuration. This frontend implementation makes no cloud or hardware calls.
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
