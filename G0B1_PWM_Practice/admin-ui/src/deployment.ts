import { DEVICE, intentSchema } from './domain'
import type { AdminApi, Intent } from './domain'
import { ApiError } from './api'

export class DeploymentRequest {
  private pending: Intent | null
  constructor(privateStorage: Storage, key: string) {
    this.storage = privateStorage; this.key = key
    const saved = this.storage.getItem(key)
    // Fail closed on corrupt/unavailable persistence rather than creating a new ID.
    this.pending = saved ? intentSchema.parse(JSON.parse(saved)) : null
  }
  private storage: Storage
  private key: string
  get current() { return this.pending }
  reconcile(ids: string[]) {
    if (this.pending && ids.includes(this.pending.request_id)) this.clear()
  }
  private clear() { this.storage.removeItem(this.key); this.pending = null }
  async submit(api: AdminApi, releaseId: string, signal: AbortSignal) {
    if (this.pending && this.pending.release_id !== releaseId) throw new Error('pending')
    const retrying = this.pending !== null
    const intent = this.pending ?? { device_id: DEVICE, release_id: releaseId, request_id: crypto.randomUUID() }
    this.storage.setItem(this.key, JSON.stringify(intent)) // Must succeed before sending.
    this.pending = intent
    try {
      const job = await api.deploy(intent, signal)
      this.clear()
      return job
    } catch (error) {
      // A rejection of a retry cannot prove the original request had no effects.
      if (!retrying && error instanceof ApiError && !error.uncertain) this.clear()
      throw error
    }
  }
}