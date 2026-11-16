import { DEVICE, GATEWAY, intentSchema } from './domain'
import type { AdminApi, Arm, Deployment, Intent, Release } from './domain'
import { ApiError } from './api'

export function demoApi(): AdminApi {
  const created = new Date(Date.now() - 3600000).toISOString()
  const releases: Release[] = [
    { release_id: '0.5.0', version: '0.5.0', target: 'stm32g0b1', size_bytes: 18432, sha256: 'a'.repeat(64), status: 'approved', created_at: created },
    { release_id: '0.4.0', version: '0.4.0', target: 'stm32g0b1', size_bytes: 17664, sha256: 'b'.repeat(64), status: 'approved', created_at: created },
    { release_id: '0.6.0', version: '0.6.0', target: 'stm32g0b1', size_bytes: 19200, sha256: 'c'.repeat(64), status: 'uploaded', created_at: created },
  ]
  let firmware = '0.4.0'
  const arms = new Map<string, Arm>()
  function armStatus(id: string) {
    const arm = arms.get(id)
    if (!arm) throw new ApiError('arm_not_found', 404)
    if (arm.status === 'armed' && Date.now() >= Date.parse(arm.expires_at)) arm.status = 'expired'
    return arm
  }
  function ready(body: Intent) {
    if (!releases.some(r => r.release_id === body.release_id && r.status === 'approved')) throw new ApiError('release_not_approved', 409)
    if (body.release_id === firmware) throw new ApiError('same_version', 409)
    if (jobs.some(j => j.status !== 'application_verified')) throw new ApiError('deployment_conflict', 409)
  }
  const jobs: Deployment[] = [{ command_id: '11111111-1111-4111-8111-111111111111', device_id: DEVICE, version: firmware, from_version: '0.3.0', status: 'application_verified', phase: 'application_verified', created_at: created, updated_at: created, flash_verified: true, application_health_verified: true }]
  return {
    async snapshot() {
      const now = new Date().toISOString()
      for (const job of jobs) {
        if (job.status === 'application_verified') continue
        const age = Date.now() - Date.parse(job.created_at)
        job.phase = age > 20000 ? 'application_verified' : age > 10000 ? 'verifying_flash' : 'downloading'
        job.status = age > 20000 ? 'application_verified' : 'running'
        job.flash_verified = age > 20000; job.application_health_verified = age > 20000
        job.updated_at = now
        if (age > 20000) firmware = job.version
      }
      return structuredClone({ devices: [
        { device_id: GATEWAY, name: GATEWAY, kind: 'gateway', parent_id: null, online: true, last_seen: now, firmware: null, state: null, updating: false },
        { device_id: DEVICE, name: DEVICE, kind: 'subdevice', parent_id: GATEWAY, online: true, last_seen: now, firmware, state: 'ready', updating: jobs.some(j => j.status !== 'application_verified') },
      ], releases, deployments: jobs })
    },
    async upload(body) {
      if (releases.some(r => r.version === body.version)) throw new Error('duplicate')
      const row: Release = { release_id: body.version, version: body.version, target: 'stm32g0b1', size_bytes: atob(body.content_base64).length, sha256: 'd'.repeat(64), status: 'uploaded', created_at: new Date().toISOString() }
      releases.unshift(row); return structuredClone(row)
    },
    async approve(id) {
      const release = releases.find(r => r.release_id === id)!
      release.status = 'approved'; return structuredClone(release)
    },
    async arm(body) {
      intentSchema.parse(body)
      if (arms.has(body.request_id)) {
        const existing = armStatus(body.request_id)
        if (existing.device_id !== body.device_id || existing.release_id !== body.release_id) throw new ApiError('request_id_reused', 409)
        return structuredClone(existing) // Same ID never renews expiry.
      }
      ready(body)
      if ([...arms.keys()].some(id => armStatus(id).status === 'armed')) throw new ApiError('arm_conflict', 409)
      const now = Date.now()
      const arm: Arm = { ...body, from_version: firmware, armed_at: new Date(now).toISOString(), expires_at: new Date(now + 120000).toISOString(), status: 'armed' }
      arms.set(body.request_id, arm)
      return structuredClone(arm)
    },
    async armStatus(id) { return structuredClone(armStatus(id)) },
    async deploy(body) {
      intentSchema.parse(body)
      const existing = jobs.find(j => j.command_id === body.request_id)
      if (existing) {
        if (existing.device_id !== body.device_id || existing.version !== body.release_id) throw new ApiError('request_id_reused', 409)
        return structuredClone(existing)
      }
      const arm = armStatus(body.request_id)
      if (arm.device_id !== body.device_id || arm.release_id !== body.release_id || arm.from_version !== firmware) throw new ApiError('arm_mismatch', 409)
      if (arm.status !== 'armed') throw new ApiError(arm.status === 'expired' ? 'arm_expired' : 'arm_consumed', 409)
      ready(body)
      arm.status = 'consumed'
      const now = new Date().toISOString()
      const job: Deployment = { command_id: body.request_id, device_id: DEVICE, version: body.release_id, from_version: firmware, status: 'dispatched', phase: 'dispatched', created_at: now, updated_at: now, flash_verified: false, application_health_verified: false }
      jobs.unshift(job); return structuredClone(job)
    },
  }
}