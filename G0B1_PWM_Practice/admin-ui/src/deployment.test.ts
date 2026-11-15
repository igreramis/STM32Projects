import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { DeploymentRequest } from './deployment'
import { testApi } from './test/fixtures'

const signal = () => new AbortController().signal
describe('durable deployment identity', () => {
  it('persists before sending and reuses the same ID after uncertainty and reload', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.mocked(api.deploy).mockImplementationOnce(async body => {
      expect(JSON.parse(sessionStorage.getItem('pending')!).request_id).toBe(body.request_id)
      throw new ApiError('service_unavailable', 503)
    })
    await expect(request.submit(api, '0.5.0', signal())).rejects.toThrow()
    const first = request.current!
    const restored = new DeploymentRequest(sessionStorage, 'pending')
    await expect(restored.submit(api, '0.6.0', signal())).rejects.toThrow()
    await restored.submit(api, '0.5.0', signal())
    expect(api.deploy).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.deploy).mock.calls[1][0]).toEqual(first)
    expect(restored.current).toBeNull()
    expect(sessionStorage.getItem('pending')).toBeNull()
  })
  it('retains identity on network failure and does not retry automatically', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.mocked(api.deploy).mockRejectedValue(new TypeError('untrusted network detail'))
    await expect(request.submit(api, '0.5.0', signal())).rejects.toThrow()
    expect(request.current).not.toBeNull()
    expect(api.deploy).toHaveBeenCalledTimes(1)
  })
  it('reconciles from history, and clears definitively rejected requests', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.mocked(api.deploy).mockRejectedValueOnce(new ApiError('network', 0))
    await expect(request.submit(api, '0.5.0', signal())).rejects.toThrow()
    request.reconcile([request.current!.request_id])
    expect(request.current).toBeNull()
    vi.mocked(api.deploy).mockRejectedValueOnce(new ApiError('device_not_ready', 409))
    await expect(request.submit(api, '0.5.0', signal())).rejects.toThrow()
    expect(request.current).toBeNull()
  })
  it('fails closed on corrupt or unavailable storage', async () => {
    sessionStorage.setItem('pending', 'broken')
    expect(() => new DeploymentRequest(sessionStorage, 'pending')).toThrow()
    sessionStorage.clear()
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('unavailable') })
    await expect(request.submit(api, '0.5.0', signal())).rejects.toThrow()
    expect(api.deploy).not.toHaveBeenCalled()
  })
  it('does not discard an uncertain original ID when a retry is rejected', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.mocked(api.deploy).mockRejectedValueOnce(new ApiError('network'))
    await expect(request.submit(api, '0.5.0', signal())).rejects.toThrow()
    const original = request.current
    vi.mocked(api.deploy).mockRejectedValueOnce(new ApiError('unauthorized', 401))
    await expect(request.submit(api, '0.5.0', signal())).rejects.toThrow()
    expect(request.current).toEqual(original)
    expect(JSON.parse(sessionStorage.getItem('pending')!)).toEqual(original)
  })
})