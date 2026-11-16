import { describe, expect, it, vi } from 'vitest'
import { demoApi } from './demo'
import { DEVICE } from './domain'

const intent = () => ({ device_id: DEVICE, release_id: '0.5.0', request_id: crypto.randomUUID() } as const)
const signal = () => new AbortController().signal
describe('isolated demo arms', () => {
  it('requires a matching arm, never installs on arm, consumes once and keeps deploy idempotency', async () => {
    const api = demoApi(), body = intent()
    await expect(api.deploy(body, signal())).rejects.toThrow()
    const before = await api.snapshot(signal())
    const arm = await api.arm(body, signal())
    expect(arm.status).toBe('armed')
    const after = await api.snapshot(signal())
    expect(after.deployments).toEqual(before.deployments)
    expect(after.devices[1].firmware).toBe(before.devices[1].firmware)
    await expect(api.deploy({ ...body, release_id: '0.6.0' }, signal())).rejects.toThrow()
    const job = await api.deploy(body, signal())
    expect((await api.armStatus(body.request_id, signal())).status).toBe('consumed')
    expect(await api.deploy(body, signal())).toEqual(job)
    expect((await api.snapshot(signal())).deployments).toHaveLength(2)
    expect(fetch).not.toHaveBeenCalled()
  })
  it('expires at 120s, never renews the same ID, and permits a fresh arm after expiry', async () => {
    vi.useFakeTimers()
    const api = demoApi(), body = intent()
    const arm = await api.arm(body, signal())
    vi.advanceTimersByTime(119000)
    expect(await api.arm(body, signal())).toEqual(arm)
    await expect(api.arm(intent(), signal())).rejects.toThrow()
    vi.advanceTimersByTime(1000)
    expect(await api.arm(body, signal())).toEqual({ ...arm, status: 'expired' })
    await expect(api.deploy(body, signal())).rejects.toThrow()
    expect((await api.arm(intent(), signal())).status).toBe('armed')
    expect(fetch).not.toHaveBeenCalled()
  })
  it('requires approval and rejects reuse of an arm for a different release', async () => {
    const api = demoApi(), body = intent()
    await expect(api.arm({ ...body, release_id: '0.6.0' }, signal())).rejects.toThrow()
    await api.arm(body, signal())
    await expect(api.arm({ ...body, release_id: '0.6.0' }, signal())).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })
})