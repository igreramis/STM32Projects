import { DEVICE, GATEWAY } from './domain'
import type { AdminApi, Deployment, Release } from './domain'

export function demoApi(): AdminApi {
  const created = new Date(Date.now() - 3600000).toISOString()
  const releases: Release[] = [
    { release_id: '0.5.0', version: '0.5.0', target: 'stm32g0b1', size_bytes: 18432, sha256: 'a'.repeat(64), status: 'approved', created_at: created },
    { release_id: '0.4.0', version: '0.4.0', target: 'stm32g0b1', size_bytes: 17664, sha256: 'b'.repeat(64), status: 'approved', created_at: created },
    { release_id: '0.6.0', version: '0.6.0', target: 'stm32g0b1', size_bytes: 19200, sha256: 'c'.repeat(64), status: 'uploaded', created_at: created },
  ]
  let firmware = '0.4.0'
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
    async deploy(body) {
      const existing = jobs.find(j => j.command_id === body.request_id)
      if (existing) return structuredClone(existing)
      const now = new Date().toISOString()
      const job: Deployment = { command_id: body.request_id, device_id: DEVICE, version: body.release_id, from_version: firmware, status: 'dispatched', phase: 'dispatched', created_at: now, updated_at: now, flash_verified: false, application_health_verified: false }
      jobs.unshift(job); return structuredClone(job)
    },
  }
}