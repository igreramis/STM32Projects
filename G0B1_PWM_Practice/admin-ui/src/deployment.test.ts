import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { DeploymentRequest } from './deployment'
import { DEVICE } from './domain'
import { armFixture, testApi } from './test/fixtures'

const signal = () => new AbortController().signal
describe('durable arm and deployment identity', () => {
  it.each([
    ['deployment_disabled', 403], ['device_not_ready', 409],
    ['release_not_approved', 409], ['same_version', 409],
  ] as const)('clears only a definitively rejected arm POST: %s', async (code, status) => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    const error = new ApiError(code, status)
    vi.mocked(api.arm).mockRejectedValueOnce(error)
    await expect(request.arm(api, '0.5.0', signal())).rejects.toBe(error)
    expect(request.current).toBeNull()
    expect(sessionStorage.getItem('pending')).toBeNull()
    expect(api.arm).toHaveBeenCalledTimes(1)
    expect(api.deploy).not.toHaveBeenCalled()
    const rejectedId = vi.mocked(api.arm).mock.calls[0][0].request_id
    await request.arm(api, '0.5.0', signal()) // A separate, explicit attempt.
    expect(request.current?.request_id).not.toBe(rejectedId)
  })
  it.each([
    ['network', 0], ['deployment_disabled', 503], ['same_version', 429],
    ['arm_not_found', 404], ['arm_conflict', 409], ['request_id_reused', 409],
    ['unauthorized', 401], ['unknown', 403],
  ] as const)('retains uncertain or conflicting arm POST outcomes: %s/%s', async (code, status) => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.mocked(api.arm).mockRejectedValue(new ApiError(code, status))
    await expect(request.arm(api, '0.5.0', signal())).rejects.toThrow()
    expect(request.current?.stage).toBe('arm_pending')
    expect(new DeploymentRequest(sessionStorage, 'pending').current).toEqual(request.current)
    await expect(request.arm(api, '0.5.0', signal())).rejects.toThrow()
    expect(api.arm).toHaveBeenCalledTimes(1)
    expect(api.deploy).not.toHaveBeenCalled()
  })
  it('preserves the request if cancellation or failed cleanup prevents accepting rejection', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    const controller = new AbortController()
    vi.mocked(api.arm).mockImplementationOnce(async () => {
      controller.abort()
      throw new ApiError('deployment_disabled', 403)
    })
    await expect(request.arm(api, '0.5.0', controller.signal)).rejects.toThrow()
    expect(request.current?.stage).toBe('arm_pending')
    const other = new DeploymentRequest(sessionStorage, 'other')
    vi.mocked(api.arm).mockRejectedValueOnce(new ApiError('deployment_disabled', 403))
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('storage unavailable') })
    await expect(other.arm(api, '0.5.0', signal())).rejects.toThrow()
    expect(other.current?.stage).toBe('arm_pending')
    expect(sessionStorage.getItem('other')).not.toBeNull()
  })
  it('persists each stage before POST and uses one exact intent for arm and dispatch', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    await expect(request.submit(api, '0.4.0', signal())).rejects.toThrow()
    expect(api.deploy).not.toHaveBeenCalled()
    vi.mocked(api.arm).mockImplementationOnce(async body => {
      expect(JSON.parse(sessionStorage.getItem('pending')!)).toEqual({ ...body, stage: 'arm_pending' })
      return armFixture(body)
    })
    await request.arm(api, '0.5.0', signal())
    expect(api.deploy).not.toHaveBeenCalled()
    const deploy = api.deploy
    api.deploy = vi.fn(async (body, abort) => {
      expect(JSON.parse(sessionStorage.getItem('pending')!)).toEqual({ ...body, stage: 'deploy_pending' })
      return deploy(body, abort)
    })
    await request.submit(api, '0.4.0', signal())
    expect(vi.mocked(api.deploy).mock.calls[0][0]).toEqual(vi.mocked(api.arm).mock.calls[0][0])
    expect(request.current).toBeNull()
    expect(sessionStorage.getItem('pending')).toBeNull()
  })
  it('reconciles an uncertain arm across reload using GET only, then permits explicit send', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.mocked(api.arm).mockImplementationOnce(async body => {
      vi.mocked(api.armStatus).mockResolvedValue(armFixture(body))
      throw new ApiError('network')
    })
    await expect(request.arm(api, '0.5.0', signal())).rejects.toThrow()
    const id = request.current!.request_id
    const restored = new DeploymentRequest(sessionStorage, 'pending')
    expect(restored.canSend('0.4.0')).toBe(false)
    await expect(restored.arm(api, '0.5.0', signal())).rejects.toThrow()
    await expect(restored.submit(api, '0.4.0', signal())).rejects.toThrow()
    await restored.reconcileArm(api, signal())
    expect(api.armStatus).toHaveBeenCalledWith(id, expect.any(AbortSignal))
    expect(restored.canSend('0.4.0')).toBe(true)
    expect(api.arm).toHaveBeenCalledTimes(1)
    expect(api.deploy).not.toHaveBeenCalled()
    await restored.submit(api, '0.4.0', signal())
    expect(vi.mocked(api.deploy).mock.calls[0][0].request_id).toBe(id)
  })
  it('revalidates a saved successful arm after reload and blocks changed firmware', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    await request.arm(api, '0.5.0', signal())
    const restored = new DeploymentRequest(sessionStorage, 'pending')
    expect(restored.canSend('0.4.0')).toBe(false)
    await restored.reconcileArm(api, signal())
    expect(restored.canSend('0.4.0')).toBe(true)
    await expect(restored.submit(api, '0.3.0', signal())).rejects.toThrow()
    expect(api.deploy).not.toHaveBeenCalled()
  })
  it('requires expiry and explicit abandonment before generating a new arm ID', async () => {
    vi.useFakeTimers()
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    await request.arm(api, '0.5.0', signal())
    const id = request.current!.request_id
    expect(() => request.abandon()).toThrow()
    vi.advanceTimersByTime(120000)
    expect(request.canSend('0.4.0')).toBe(false)
    await expect(request.submit(api, '0.4.0', signal())).rejects.toThrow()
    await expect(request.arm(api, '0.5.0', signal())).rejects.toThrow()
    request.abandon()
    expect(api.arm).toHaveBeenCalledTimes(1)
    await request.arm(api, '0.5.0', signal())
    expect(request.current!.request_id).not.toBe(id)
    expect(api.deploy).not.toHaveBeenCalled()
  })
  it.each([0, 401, 409, 503])('never retries or abandons attempted dispatch, even after expiry/reload (%s)', async status => {
    vi.useFakeTimers()
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    await request.arm(api, '0.5.0', signal())
    vi.mocked(api.deploy).mockRejectedValue(new ApiError('failed', status))
    await expect(request.submit(api, '0.4.0', signal())).rejects.toThrow()
    const original = request.current!
    vi.advanceTimersByTime(121000)
    const restored = new DeploymentRequest(sessionStorage, 'pending')
    expect(restored.current).toEqual(original)
    expect(() => restored.abandon()).toThrow()
    await expect(restored.arm(api, '0.6.0', signal())).rejects.toThrow()
    await expect(restored.submit(api, '0.4.0', signal())).rejects.toThrow()
    await restored.reconcileArm(api, signal())
    restored.reconcile([])
    expect(restored.current).toEqual(original)
    expect(api.armStatus).not.toHaveBeenCalled()
    expect(api.arm).toHaveBeenCalledTimes(1)
    expect(api.deploy).toHaveBeenCalledTimes(1)
    restored.reconcile([original.request_id])
    expect(restored.current).toBeNull()
  })
  it('treats legacy pending intents as uncertain dispatches, never as new arms', async () => {
    const intent = { device_id: DEVICE, release_id: '0.5.0', request_id: crypto.randomUUID() }
    sessionStorage.setItem('pending', JSON.stringify(intent))
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    expect(request.current).toEqual({ ...intent, stage: 'deploy_pending' })
    await expect(request.arm(api, '0.5.0', signal())).rejects.toThrow()
    await expect(request.submit(api, '0.4.0', signal())).rejects.toThrow()
    expect(() => request.abandon()).toThrow()
    expect(api.arm).not.toHaveBeenCalled()
    expect(api.deploy).not.toHaveBeenCalled()
  })
  it.each(['broken', '', 'null', '{}', '{"stage":"unknown"}'])('fails closed on corrupt storage: %s', saved => {
    sessionStorage.setItem('pending', saved)
    expect(() => new DeploymentRequest(sessionStorage, 'pending')).toThrow()
  })
  it('does not send if persistence fails before either POST', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    const setter = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('unavailable') })
    await expect(request.arm(api, '0.5.0', signal())).rejects.toThrow()
    expect(api.arm).not.toHaveBeenCalled()
    setter.mockRestore()
    await request.arm(api, '0.5.0', signal())
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('unavailable') })
    await expect(request.submit(api, '0.4.0', signal())).rejects.toThrow()
    expect(api.deploy).not.toHaveBeenCalled()
  })
  it('keeps unknown/mismatched arm outcomes locked and sends consumed arms to history reconciliation', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    vi.mocked(api.arm).mockRejectedValue(new ApiError('network'))
    await expect(request.arm(api, '0.5.0', signal())).rejects.toThrow()
    vi.mocked(api.armStatus).mockRejectedValueOnce(new ApiError('arm_not_found', 404))
    await expect(request.reconcileArm(api, signal())).rejects.toThrow()
    expect(() => request.abandon()).toThrow()
    const arm = armFixture(request.current!)
    vi.mocked(api.armStatus).mockResolvedValueOnce({ ...arm, request_id: crypto.randomUUID() })
    await expect(request.reconcileArm(api, signal())).rejects.toThrow()
    expect(request.canSend('0.4.0')).toBe(false)
    vi.mocked(api.armStatus).mockResolvedValueOnce({ ...arm, status: 'consumed' })
    await request.reconcileArm(api, signal())
    expect(request.current?.stage).toBe('deploy_pending')
    expect(() => request.abandon()).toThrow()
  })
  it('ignores an in-flight arm GET that completes after dispatch was attempted', async () => {
    const api = testApi(), request = new DeploymentRequest(sessionStorage, 'pending')
    await request.arm(api, '0.5.0', signal())
    const arm = armFixture(request.current!)
    let resolve!: (value: typeof arm) => void
    vi.mocked(api.armStatus).mockReturnValue(new Promise(done => { resolve = done }))
    const reading = request.reconcileArm(api, signal())
    vi.mocked(api.deploy).mockRejectedValue(new ApiError('network'))
    await expect(request.submit(api, '0.4.0', signal())).rejects.toThrow()
    resolve(arm); await reading
    expect(request.current?.stage).toBe('deploy_pending')
  })
})