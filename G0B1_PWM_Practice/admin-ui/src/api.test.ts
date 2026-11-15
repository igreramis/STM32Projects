import { describe, expect, it, vi } from 'vitest'
import { ApiError, realApi } from './api'
import { createRuntime, oidcSettings } from './auth'
import { DEVICE } from './domain'
import { snapshot } from './test/fixtures'

describe('real API and auth boundaries', () => {
  it('uses access-token authorization and exact API routes', async () => {
    const data = snapshot()
    vi.mocked(fetch).mockImplementation(async input => {
      const path = String(input)
      return new Response(JSON.stringify(path.endsWith('/devices') ? { devices: data.devices } : path.endsWith('/releases') ? { releases: data.releases } : { deployments: [] }))
    })
    const api = realApi('https://api.example.test/', async () => 'test-access-token')
    expect(await api.snapshot(new AbortController().signal)).toEqual(data)
    expect(fetch).toHaveBeenCalledTimes(3)
    for (const call of vi.mocked(fetch).mock.calls) {
      expect(call[1]?.headers).toEqual({ Authorization: 'Bearer test-access-token' })
      expect(call[1]?.credentials).toBe('omit')
      expect(call[1]?.redirect).toBe('error')
    }
  })
  it('never exposes untrusted server errors, URLs or tokens', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ error: 'https://secret.example/token?token=private' }), { status: 503 }))
    const api = realApi('https://api.example.test', async () => 'test')
    await expect(api.approve('0.5.0', new AbortController().signal)).rejects.toThrow('The service could not complete this request.')
  })
  it('rejects malformed success responses as uncertain', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('{}'))
    const api = realApi('https://api.example.test', async () => 'test')
    await expect(api.deploy({ device_id: DEVICE, release_id: '0.5.0', request_id: crypto.randomUUID() }, new AbortController().signal)).rejects.toMatchObject({ uncertain: true })
  })
  it('does not send requests when the session is expired', async () => {
    const api = realApi('https://api.example.test', async () => { throw new ApiError('unauthorized', 401) })
    await expect(api.snapshot(new AbortController().signal)).rejects.toMatchObject({ unauthorized: true })
    expect(fetch).not.toHaveBeenCalled()
  })
  it('configures authorization code PKCE and session stores with the API scope', async () => {
    const config = { cognitoDomain: 'https://example.auth.ap-southeast-2.amazoncognito.com', apiBaseUrl: 'https://api.example.test', userPoolClientId: 'testclient', redirectUri: 'http://localhost:5173/', region: 'ap-southeast-2' }
    const settings = oidcSettings(config, sessionStorage)
    expect(settings.response_type).toBe('code')
    expect(settings.disablePKCE).toBe(false)
    expect(settings.scope).toContain('admin-api/access')
    expect(settings.loadUserInfo).toBe(false)
    await settings.userStore.set('token', 'session-only')
    expect(sessionStorage.getItem('oidc.token')).toBe('session-only')
    expect(localStorage.length).toBe(0)
  })
  it('fails closed on missing runtime config, never silently enters demo', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 404 }))
    await expect(createRuntime('')).rejects.toThrow()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/config.json')
  })
})
describe('explicit demo boundary', () => {
  it('performs every operation with fixtures, never fetching config or cloud', async () => {
    const app = await createRuntime('?demo=1'), signal = new AbortController().signal
    expect(app.demo).toBe(true)
    await app.login(); await app.logout()
    await app.api.snapshot(signal)
    await app.api.upload({ version: '0.7.0', filename: 'f.bin', content_base64: btoa('fixture!') }, signal)
    await app.api.approve('0.7.0', signal)
    const job = await app.api.deploy({ device_id: DEVICE, release_id: '0.7.0', request_id: crypto.randomUUID() }, signal)
    expect(job.version).toBe('0.7.0')
    expect(fetch).not.toHaveBeenCalled()
    expect(localStorage.length).toBe(0)
  })
  it.each(['?demo=0', '?demo=true', '?mode=demo'])('requires exact explicit opt-in: %s', async query => {
    await expect(createRuntime(query)).rejects.toThrow()
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})