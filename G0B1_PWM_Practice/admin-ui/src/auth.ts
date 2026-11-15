import { Log, UserManager, WebStorageStateStore } from 'oidc-client-ts'
import { z } from 'zod'
import { ApiError, fetchBounded, realApi } from './api'
import { demoApi } from './demo'
import type { AdminApi } from './domain'

const httpsUrl = z.string().url().refine(value => {
  const u = new URL(value)
  return u.protocol === 'https:' && !u.username && !u.password && !u.search && !u.hash
})
export const configSchema = z.object({
  apiBaseUrl: httpsUrl, cognitoDomain: httpsUrl.refine(value => new URL(value).pathname === '/'),
  userPoolClientId: z.string().regex(/^[a-z0-9]{1,128}$/),
  redirectUri: z.string().url().refine(value => {
    const u = new URL(value)
    return !u.username && !u.password && !u.search && !u.hash && (u.protocol === 'https:' || (u.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(u.hostname)))
  }), region: z.string().regex(/^[a-z]{2}(?:-[a-z]+)+-\d$/),
})
export type Runtime = { demo: boolean; api: AdminApi; storageKey: string; login: () => Promise<void>; logout: () => Promise<void>; authenticated: boolean }
export function oidcSettings(config: z.infer<typeof configSchema>, storage: Storage) {
  const domain = config.cognitoDomain.replace(/\/$/, '')
  return {
    authority: domain, client_id: config.userPoolClientId, redirect_uri: config.redirectUri,
    response_type: 'code', scope: 'openid profile admin-api/access', disablePKCE: false,
    automaticSilentRenew: false, loadUserInfo: false, monitorSession: false,
    // Hosted Cognito domains do not expose discovery. The five-field runtime
    // contract supplies the domain, so configure its standard OAuth endpoints.
    metadata: { authorization_endpoint: `${domain}/oauth2/authorize`, token_endpoint: `${domain}/oauth2/token`, revocation_endpoint: `${domain}/oauth2/revoke` },
    userStore: new WebStorageStateStore({ store: storage }), stateStore: new WebStorageStateStore({ store: storage }),
  }
}
export async function createRuntime(search = location.search): Promise<Runtime> {
  // Branch before config fetch, OIDC construction, or token access. No fallback to demo.
  if (new URLSearchParams(search).get('demo') === '1') return {
    demo: true, api: demoApi(), storageKey: 'ota-demo-pending', authenticated: true,
    login: async () => {}, logout: async () => {},
  }
  const response = await fetchBounded('/config.json')
  if (!response.ok) throw new Error('configuration')
  const config = configSchema.parse(await response.json())
  if (new URL(config.redirectUri).origin !== location.origin) throw new Error('configuration')
  Log.setLevel(Log.NONE)
  const manager = new UserManager(oidcSettings(config, sessionStorage))
  const params = new URLSearchParams(search)
  if (params.has('code') || params.has('error')) {
    try { await manager.signinRedirectCallback() }
    finally { history.replaceState({}, '', location.pathname) }
  }
  const user = await manager.getUser()
  const login = async () => { await manager.clearStaleState(); await manager.signinRedirect() }
  return {
    demo: false, authenticated: Boolean(user && !user.expired),
    storageKey: `ota-pending:${config.apiBaseUrl}:${config.userPoolClientId}:${user?.profile.sub ?? 'anonymous'}`,
    api: realApi(config.apiBaseUrl, async () => {
      const current = await manager.getUser()
      if (!current || current.expired || !current.access_token) throw new ApiError('unauthorized', 401)
      return current.access_token
    }), login,
    logout: async () => {
      await manager.removeUser()
      manager.stopSilentRenew()
      const url = new URL(`${config.cognitoDomain.replace(/\/$/, '')}/logout`)
      url.searchParams.set('client_id', config.userPoolClientId)
      url.searchParams.set('logout_uri', config.redirectUri)
      location.assign(url.toString())
    },
  }
}