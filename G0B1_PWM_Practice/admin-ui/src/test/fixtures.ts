import type { AdminApi, Snapshot } from '../domain'
import type { Runtime } from '../auth'
import { vi } from 'vitest'

export function snapshot(): Snapshot {
  const now = new Date().toISOString()
  return {
    devices: [
      { device_id: 'pi-lab-01', name: 'pi-lab-01', kind: 'gateway', parent_id: null, online: true, last_seen: now, firmware: null, state: null, updating: false },
      { device_id: 'stm32g0b1-01', name: 'stm32g0b1-01', kind: 'subdevice', parent_id: 'pi-lab-01', online: true, last_seen: now, firmware: '0.4.0', state: 'running', updating: false },
    ],
    releases: [
      { release_id: '0.5.0', version: '0.5.0', target: 'stm32g0b1', size_bytes: 1024, sha256: 'a'.repeat(64), status: 'approved', created_at: now },
      { release_id: '0.6.0', version: '0.6.0', target: 'stm32g0b1', size_bytes: 1024, sha256: 'b'.repeat(64), status: 'uploaded', created_at: now },
    ], deployments: [],
  }
}
export function testApi(data = snapshot()): AdminApi {
  return {
    snapshot: vi.fn(async () => structuredClone(data)),
    upload: vi.fn(async () => data.releases[1]),
    approve: vi.fn(async () => ({ ...data.releases[1], status: 'approved' as const })),
    deploy: vi.fn(async body => ({ command_id: body.request_id, device_id: body.device_id, version: body.release_id, from_version: '0.4.0', status: 'dispatched', phase: 'dispatched', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), flash_verified: false, application_health_verified: false })),
  }
}
export function runtime(api = testApi(), demo = false): Runtime {
  return { demo, api, authenticated: true, storageKey: demo ? 'test-demo' : 'test-live', login: vi.fn(async () => {}), logout: vi.fn(async () => {}) }
}
export function binaryFile(version = '0.5.0', filename = 'firmware.bin', size = 128): File {
  const bytes = new Uint8Array(size)
  const view = new DataView(bytes.buffer)
  view.setUint32(0, 0x20001000, true); view.setUint32(4, 0x08000009, true)
  bytes.set(new TextEncoder().encode(`firmware=${version}\0`), 16)
  return new File([bytes], filename, { type: 'application/octet-stream' })
}