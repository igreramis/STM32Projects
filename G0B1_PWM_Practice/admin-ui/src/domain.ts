import { z } from 'zod'

export const GATEWAY = 'pi-lab-01'
export const DEVICE = 'stm32g0b1-01'
export const semver = /^(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})$/
const version = z.string().regex(semver)
const date = z.iso.datetime({ offset: true })
export const deviceSchema = z.object({
  device_id: z.enum([GATEWAY, DEVICE]), name: z.string(), kind: z.enum(['gateway', 'subdevice']),
  parent_id: z.string().nullable(), online: z.boolean(), last_seen: date.nullable(),
  firmware: version.nullable(), state: z.enum(['idle', 'ready', 'running', 'paused', 'fault']).nullable(), updating: z.boolean(),
})
export const releaseSchema = z.object({
  release_id: version, version, target: z.literal('stm32g0b1'), size_bytes: z.number().int().min(8).max(20480),
  sha256: z.string().regex(/^[a-f0-9]{64}$/), status: z.enum(['uploaded', 'approved']), created_at: date,
})
export const deploymentSchema = z.object({
  command_id: z.uuid(), device_id: z.literal(DEVICE), version, from_version: version,
  status: z.string(), phase: z.string(), created_at: date, updated_at: date,
  error: z.string().optional(), flash_verified: z.boolean().optional(), application_health_verified: z.boolean().optional(),
})
export type Device = z.infer<typeof deviceSchema>
export type Release = z.infer<typeof releaseSchema>
export type Deployment = z.infer<typeof deploymentSchema>
export type Snapshot = { devices: Device[]; releases: Release[]; deployments: Deployment[] }
export const intentSchema = z.object({ device_id: z.literal(DEVICE), release_id: version, request_id: z.uuid() })
export type Intent = z.infer<typeof intentSchema>
export type Upload = { version: string; filename: string; content_base64: string }
export interface AdminApi {
  snapshot(signal: AbortSignal): Promise<Snapshot>
  upload(body: Upload, signal: AbortSignal): Promise<Release>
  approve(id: string, signal: AbortSignal): Promise<Release>
  deploy(body: Intent, signal: AbortSignal): Promise<Deployment>
}

export function fresh(device: Device | undefined, now = Date.now()): boolean {
  if (!device?.online || !device.last_seen) return false
  const age = now - Date.parse(device.last_seen)
  return age >= 0 && age <= 35000
}
export function connected(device: Device, devices: Device[], now = Date.now()): boolean {
  return fresh(device, now) && (device.kind === 'gateway' || fresh(devices.find(d => d.device_id === device.parent_id), now))
}
export function active(job: Deployment): boolean {
  return job.phase === 'dispatch_unknown' || !['application_verified', 'error', 'interrupted', 'rejected'].includes(job.status)
}
export function deployBlock(data: Snapshot, selected: string, releaseId: string, now = Date.now()): string | null {
  const device = data.devices.find(d => d.device_id === selected)
  const release = data.releases.find(r => r.release_id === releaseId)
  if (selected !== DEVICE || !device) return 'Select the subdevice to deploy firmware.'
  if (!release || release.status !== 'approved') return 'Select an approved release.'
  if (!connected(device, data.devices, now)) return 'Fresh gateway and subdevice observations are required.'
  if (!device.firmware || !semver.test(device.firmware)) return 'Current firmware must be verified by the gateway.'
  if (device.updating || data.deployments.some(active)) return 'An update is in progress or requires investigation.'
  if (device.firmware === release.version) return 'This version is already installed.'
  return null
}

export const phases = ['dispatched', 'accepted', 'preparing', 'downloading', 'image_validated', 'waiting_for_rom', 'validating_target', 'backing_up', 'backup_saved', 'erase_started', 'programming', 'verifying_flash', 'flash_verified', 'waiting_for_application', 'application_verified']
const phaseLabels = ['Command sent', 'Accepted', 'Preparing', 'Downloading', 'Image validated', 'Awaiting device', 'Validating target', 'Creating backup', 'Backup saved', 'Preparing storage', 'Programming', 'Verifying flash', 'Flash verified', 'Checking application', 'Application verified']
export function phaseLabel(job: Deployment): string {
  if (job.phase === 'dispatch_unknown') return 'Dispatch uncertain · investigation required'
  if (['error', 'interrupted', 'rejected'].includes(job.status)) return { error: 'Update failed', interrupted: 'Update interrupted', rejected: 'Update rejected' }[job.status]!
  return phaseLabels[phases.indexOf(job.phase)] ?? 'Awaiting status'
}
export function progress(job: Deployment): number {
  // Progress describes reported stages, not elapsed time or hardware proof.
  return Math.max(0, Math.round((phases.indexOf(job.phase) + 1) / phases.length * 100))
}

export function validateFile(file: Pick<File, 'name' | 'size'> | null, v: string): string | null {
  if (!semver.test(v)) return 'Use a numeric version such as 0.5.0 (no prefixes or prereleases).'
  if (!file) return 'Choose a firmware binary.'
  if (!/^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\.bin$/.test(file.name)) return 'Use a .bin filename with only letters, numbers, underscores, dots or hyphens (5–104 characters).'
  if (file.size < 8 || file.size > 20480) return 'Binary size must be between 8 bytes and 20 KiB.'
  return null
}
export async function prepareUpload(file: File, v: string): Promise<Upload> {
  const error = validateFile(file, v)
  if (error) throw new Error(error)
  const bytes = new Uint8Array(await file.arrayBuffer())
  const view = new DataView(bytes.buffer)
  const sp = view.getUint32(0, true), reset = view.getUint32(4, true)
  if (sp < 0x20000000 || sp > 0x20024000 || sp % 8 || !(reset & 1) || (reset & ~1) < 0x08000008 || (reset & ~1) >= 0x08000000 + bytes.length) throw new Error('The binary has invalid vector entries.')
  const text = Array.from(bytes, b => String.fromCharCode(b)).join('')
  // eslint-disable-next-line no-control-regex -- binary tokens end at NUL or ASCII whitespace, matching the server.
  const tokens = [...text.matchAll(/firmware=([^\x00\x09-\x0d\x20]+)/g)] // ASCII whitespace matches the server's byte validation.
  if (!tokens.length || tokens.some(t => t[1] !== v)) throw new Error('The embedded firmware version must match the release version.')
  return { version: v, filename: file.name, content_base64: btoa(text) }
}