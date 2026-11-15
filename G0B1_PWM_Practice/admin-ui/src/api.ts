import { z } from 'zod'
import { deploymentSchema, deviceSchema, releaseSchema } from './domain'
import type { AdminApi } from './domain'

const messages: Record<string, string> = {
  unauthorized: 'Your session has expired. Sign in again.', forbidden: 'An administrator account with API access is required.',
  duplicate_version: 'This version has already been reserved. Use a new version.', invalid_upload: 'The upload does not meet the filename or binary requirements.',
  invalid_firmware: 'The firmware binary is not valid.', invalid_vectors: 'The binary has invalid vector entries.',
  firmware_version_mismatch: 'The embedded firmware version does not match.', release_not_approved: 'The release must be approved before deployment.',
  device_not_ready: 'The gateway or subdevice is not ready. Wait for fresh observations.', same_version: 'This firmware version is already installed.',
  deployment_conflict: 'Another deployment is active or requires investigation.', conflict: 'The data changed. Refresh and review before continuing.',
  release_integrity_failed: 'Release integrity validation failed. Approval was not completed.', release_not_found: 'The release is no longer available.',
  request_id_reused: 'This request belongs to another deployment. Investigation is required.',
}
export class ApiError extends Error {
  readonly uncertain: boolean
  readonly unauthorized: boolean
  constructor(code: string, status = 0) {
    super(messages[code] ?? (status === 0 ? 'The service could not be reached. Check your connection.' : 'The service could not complete this request.'))
    this.uncertain = status === 0 || status >= 500 || status === 408 || status === 429
    this.unauthorized = status === 401
  }
}
export async function fetchBounded(url: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 15000)
  const cancel = () => controller.abort()
  init.signal?.addEventListener('abort', cancel, { once: true })
  if (init.signal?.aborted) controller.abort()
  try {
    const response = await fetch(url, { ...init, signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' })
    // Keep the timeout active through body consumption, not only response headers.
    const body = await response.arrayBuffer()
    return new Response(response.status === 204 ? null : body, { status: response.status, statusText: response.statusText, headers: response.headers })
  }
  finally { clearTimeout(timer); init.signal?.removeEventListener('abort', cancel) }
}
export function realApi(baseUrl: string, token: () => Promise<string>): AdminApi {
  async function request<T>(path: string, schema: z.ZodType<T>, signal: AbortSignal, body?: unknown): Promise<T> {
    try {
      const accessToken = await token()
      const response = await fetchBounded(`${baseUrl.replace(/\/$/, '')}/api/${path}`, {
        signal, method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${accessToken}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      if (!response.ok) {
        const error = await response.json().catch(() => null)
        throw new ApiError(typeof error?.error === 'string' ? error.error : '', response.status)
      }
      const parsed = schema.safeParse(await response.json())
      if (!parsed.success) throw new ApiError('invalid_response')
      return parsed.data
    } catch (error) { throw error instanceof ApiError ? error : new ApiError('network_error') }
  }
  return {
    async snapshot(signal) {
      const [devices, releases, deployments] = await Promise.all([
        request('devices', z.object({ devices: z.array(deviceSchema) }), signal),
        request('releases', z.object({ releases: z.array(releaseSchema) }), signal),
        request('deployments', z.object({ deployments: z.array(deploymentSchema) }), signal),
      ])
      return { ...devices, ...releases, ...deployments }
    },
    upload: (body, signal) => request('releases', releaseSchema, signal, body),
    approve: (id, signal) => request(`releases/${encodeURIComponent(id)}/approve`, releaseSchema, signal, {}),
    deploy: (body, signal) => request('deployments', deploymentSchema, signal, body),
  }
}