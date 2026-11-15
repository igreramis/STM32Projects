import { describe, expect, it } from 'vitest'
import { active, connected, deployBlock, DEVICE, fresh, phaseLabel, prepareUpload, validateFile } from './domain'
import { binaryFile, snapshot, testApi } from './test/fixtures'

describe('observation freshness and deployment gates', () => {
  it('keeps running state independent of stale connectivity', () => {
    const data = snapshot(), device = data.devices[1]
    device.last_seen = new Date(Date.now() - 36000).toISOString()
    expect(connected(device, data.devices)).toBe(false)
    expect(device.state).toBe('running')
    expect(deployBlock(data, DEVICE, '0.5.0')).toMatch(/Fresh/)
  })
  it('requires a fresh gateway, rejects future and absent observations', () => {
    const data = snapshot()
    data.devices[0].online = false
    expect(connected(data.devices[1], data.devices)).toBe(false)
    expect(fresh({ ...data.devices[1], last_seen: new Date(Date.now() + 1000).toISOString() })).toBe(false)
    expect(fresh({ ...data.devices[1], last_seen: null })).toBe(false)
  })
  it('accepts exactly 35 seconds but no older', () => {
    const device = snapshot().devices[1], t = Date.parse(device.last_seen!)
    expect(fresh(device, t + 35000)).toBe(true)
    expect(fresh(device, t + 35001)).toBe(false)
  })
  it('requires sole subdevice, approved release, current firmware and no update', () => {
    const data = snapshot()
    expect(deployBlock(data, DEVICE, '0.5.0')).toBeNull()
    expect(deployBlock(data, 'pi-lab-01', '0.5.0')).toMatch(/subdevice/)
    expect(deployBlock(data, DEVICE, '0.6.0')).toMatch(/approved/)
    expect(deployBlock(data, DEVICE, '')).toMatch(/approved/)
    data.devices[1].firmware = null
    expect(deployBlock(data, DEVICE, '0.5.0')).toMatch(/verified/)
    data.devices[1].firmware = '0.5.0'
    expect(deployBlock(data, DEVICE, '0.5.0')).toMatch(/already/)
    data.devices[1].updating = true
    expect(deployBlock(data, DEVICE, '0.5.0')).toMatch(/progress/)
  })
  it('treats dispatch_unknown as active even when status is error', async () => {
    const data = snapshot()
    const job = await testApi().deploy({ device_id: DEVICE, release_id: '0.5.0', request_id: crypto.randomUUID() }, new AbortController().signal)
    job.status = 'error'; job.phase = 'dispatch_unknown'; data.deployments.push(job)
    expect(active(job)).toBe(true)
    expect(deployBlock(data, DEVICE, '0.5.0')).toMatch(/investigation/)
    expect(phaseLabel(job)).toMatch(/uncertain/)
  })
})
describe('firmware constraints', () => {
  it.each(['v0.5.0', '01.5.0', '0.5', '0.5.0-rc1', '1000000000.0.0'])('rejects version %s', v => {
    expect(validateFile({ name: 'firmware.bin', size: 100 }, v)).toMatch(/numeric/)
  })
  it.each(['firmware.hex', '../firmware.bin', '.bin', 'bad name.bin', 'a'.repeat(101) + '.bin'])('rejects filename %s', name => {
    expect(validateFile({ name, size: 100 }, '0.5.0')).toMatch(/filename/)
  })
  it.each([0, 7, 20481])('rejects size %s', size => {
    expect(validateFile({ name: 'firmware.bin', size }, '0.5.0')).toMatch(/size/)
  })
  it('accepts the inclusive size boundaries', () => {
    expect(validateFile({ name: 'f.bin', size: 8 }, '0.5.0')).toBeNull()
    expect(validateFile({ name: 'f.bin', size: 20480 }, '0.5.0')).toBeNull()
  })
  it('encodes a valid binary and rejects vectors or version mismatches', async () => {
    const body = await prepareUpload(binaryFile(), '0.5.0')
    expect(atob(body.content_base64)).toContain('firmware=0.5.0\0')
    await expect(prepareUpload(binaryFile('0.5.01'), '0.5.0')).rejects.toThrow(/embedded/)
    await expect(prepareUpload(new File([new Uint8Array(64)], 'f.bin'), '0.5.0')).rejects.toThrow(/vector/)
  })
})