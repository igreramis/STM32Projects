import { z } from 'zod'
import { ApiError } from './api'
import { armSchema, DEVICE, intentSchema } from './domain'
import type { AdminApi, Arm, Intent } from './domain'

const savedSchema = z.discriminatedUnion('stage', [
  intentSchema.extend({ stage: z.literal('arm_pending') }).strict(),
  intentSchema.extend({ stage: z.literal('armed'), arm: armSchema }).strict(),
  intentSchema.extend({ stage: z.literal('deploy_pending') }).strict(),
])
type Saved = z.infer<typeof savedSchema>
const matches = (intent: Intent, arm: Arm) => intent.request_id === arm.request_id && intent.device_id === arm.device_id && intent.release_id === arm.release_id

export class DeploymentRequest {
  private pending: Saved | null = null
  private storage: Storage
  private key: string
  constructor(storage: Storage, key: string) {
    this.storage = storage; this.key = key
    const saved = storage.getItem(key)
    if (saved !== null) {
      const raw: unknown = JSON.parse(saved)
      // Legacy bare intents may already have dispatched. Never migrate them to an arm.
      const legacy = intentSchema.strict().safeParse(raw)
      const parsed: Saved = legacy.success ? { ...legacy.data, stage: 'deploy_pending' } : savedSchema.parse(raw)
      if (parsed.stage === 'armed' && !matches(parsed, parsed.arm)) throw new Error('invalid persistence')
      // Reload requires a read-only server check, not trust in a saved arm.
      this.pending = parsed.stage === 'armed' ? { ...intentSchema.parse(parsed), stage: 'arm_pending' } : parsed
    }
  }
  get current() { return this.pending }
  private save(value: Saved) {
    this.storage.setItem(this.key, JSON.stringify(value)) // Must succeed before any POST.
    this.pending = value
  }
  private clear() { this.storage.removeItem(this.key); this.pending = null }
  reconcile(ids: string[]) {
    if (this.pending?.stage === 'deploy_pending' && ids.includes(this.pending.request_id)) this.clear()
  }
  private acceptArm(pending: Saved, response: Arm) {
    const arm = armSchema.parse(response)
    if (!matches(pending, arm)) throw new Error('arm mismatch')
    if (this.pending !== pending) return // A read cannot undo a concurrent dispatch.
    const intent = intentSchema.parse(pending)
    this.save(arm.status === 'consumed' ? { ...intent, stage: 'deploy_pending' } : { ...intent, stage: 'armed', arm })
  }
  async reconcileArm(api: AdminApi, signal: AbortSignal) {
    const pending = this.pending
    if (!pending || pending.stage === 'deploy_pending') return
    const arm = await api.armStatus(pending.request_id, signal)
    if (!signal.aborted) this.acceptArm(pending, arm)
  }
  async arm(api: AdminApi, releaseId: string, signal: AbortSignal) {
    if (this.pending || signal.aborted) throw new Error('pending')
    const intent: Intent = intentSchema.parse({ device_id: DEVICE, release_id: releaseId, request_id: crypto.randomUUID() })
    const pending: Saved = { ...intent, stage: 'arm_pending' }
    this.save(pending)
    let response: Arm
    try {
      response = await api.arm(intent, signal) // Never retry, even after uncertainty.
    } catch (error) {
      // Only these POST rejections occur before any arm write. A GET 404,
      // conflict, aborted request or transport failure is NOT proof of rejection.
      const rejected = error instanceof ApiError && (
        (error.status === 403 && error.code === 'deployment_disabled') ||
        (error.status === 409 && ['device_not_ready', 'release_not_approved', 'same_version'].includes(error.code))
      )
      if (rejected && !signal.aborted && this.pending === pending) this.clear()
      throw error
    }
    if (!signal.aborted) this.acceptArm(pending, response)
  }
  canSend(fromVersion: string | null | undefined, now = Date.now()) {
    const pending = this.pending
    return pending?.stage === 'armed' && pending.arm.status === 'armed' && pending.arm.from_version === fromVersion &&
      Date.parse(pending.arm.armed_at) <= now && now < Date.parse(pending.arm.expires_at)
  }
  canAbandon(now = Date.now()) {
    return this.pending?.stage === 'armed' && (this.pending.arm.status === 'expired' || now >= Date.parse(this.pending.arm.expires_at))
  }
  abandon() {
    if (!this.canAbandon()) throw new Error('cannot abandon')
    this.clear()
  }
  async submit(api: AdminApi, fromVersion: string | null | undefined, signal: AbortSignal) {
    if (!this.canSend(fromVersion) || signal.aborted) throw new Error('not armed')
    const intent = intentSchema.parse(this.pending)
    this.save({ ...intent, stage: 'deploy_pending' })
    // Once attempted, only deployment-history reconciliation may release the ID.
    const job = await api.deploy(intent, signal)
    if (job.command_id !== intent.request_id || job.device_id !== intent.device_id || job.version !== intent.release_id) throw new Error('deployment mismatch')
    if (!signal.aborted) this.clear()
    return job
  }
}