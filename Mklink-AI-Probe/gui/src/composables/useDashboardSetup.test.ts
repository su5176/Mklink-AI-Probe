// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { loadDesktopSettings, saveDesktopSettings } from '../lib/desktopSettings'

class MemoryStorage implements Storage {
  private values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, String(value)) }
}

const storage = new MemoryStorage()

const mocks = vi.hoisted(() => ({
  deviceStatus: {
    __v_isRef: true,
    value: { connected: false, axf: { loaded: false, axf_path: null as string | null } },
  },
  connectDevice: vi.fn(),
  disconnectDevice: vi.fn(),
  parseAxf: vi.fn(),
  findRtt: vi.fn(),
  uploadFileSource: vi.fn(),
  ensureLoaded: vi.fn(),
  pickSymbolFile: vi.fn(),
}))

vi.mock('./useMklinkApi', () => ({
  useMklinkApi: () => ({
    deviceStatus: mocks.deviceStatus,
    connectDevice: mocks.connectDevice,
    disconnectDevice: mocks.disconnectDevice,
    parseAxf: mocks.parseAxf,
    findRtt: mocks.findRtt,
    uploadFileSource: mocks.uploadFileSource,
  }),
}))

vi.mock('./useSymbolCatalog', () => ({
  useSymbolCatalog: () => ({ ensureLoaded: mocks.ensureLoaded }),
}))

vi.mock('./useToast', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}))

vi.mock('../lib/filePicker', () => ({
  pickSymbolFile: mocks.pickSymbolFile,
}))

import { useDashboardSetup } from './useDashboardSetup'

describe('useDashboardSetup', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    storage.clear()
    vi.stubGlobal('localStorage', storage)
    Object.defineProperty(window, 'localStorage', { value: storage, configurable: true })
    saveDesktopSettings(storage, {
      version: 1,
      symbolPath: '',
      symbolDisplayPath: '',
      rttAddress: '',
      rttEncoding: 'utf-8',
      transmitMode: 'text',
      lineEnding: '',
      sendHistory: [],
    })
    mocks.deviceStatus.value.connected = false
    mocks.deviceStatus.value.axf = { loaded: false, axf_path: null }
    mocks.findRtt.mockResolvedValue({ found: false, addr: null })
  })

  it('quick-connects with the last successful backend settings and current symbol file', async () => {
    const settings = loadDesktopSettings(storage)
    saveDesktopSettings(storage, { ...settings, symbolPath: 'C:\\firmware\\app.axf' })
    mocks.connectDevice.mockResolvedValue({ connected: true })

    await useDashboardSetup().quickConnect()

    expect(mocks.connectDevice).toHaveBeenCalledWith({
      restore_last: true,
      axf: 'C:\\firmware\\app.axf',
    })
  })

  it('loads a chosen AXF immediately and refreshes every symbol consumer', async () => {
    mocks.deviceStatus.value.connected = true
    mocks.pickSymbolFile.mockResolvedValue('C:\\firmware\\app.axf')
    mocks.parseAxf.mockResolvedValue({
      loaded: true,
      axf_path: 'C:\\firmware\\app.axf',
      variable_count: 12,
    })
    mocks.ensureLoaded.mockResolvedValue(undefined)
    const current = loadDesktopSettings(storage)
    saveDesktopSettings(storage, { ...current, rttAddress: '0x20000010' })
    mocks.findRtt.mockResolvedValue({ found: true, addr: '0x20001A40' })

    const result = await useDashboardSetup().loadSymbolFile()

    expect(result).toBe(true)
    expect(loadDesktopSettings(storage).symbolPath).toBe('C:\\firmware\\app.axf')
    expect(mocks.parseAxf).toHaveBeenCalledWith('C:\\firmware\\app.axf')
    expect(mocks.findRtt).toHaveBeenCalledWith('C:\\firmware\\app.axf')
    expect(mocks.ensureLoaded).toHaveBeenCalledWith(true)
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20001A40')
  })

  it('clears a stale RTT address as soon as a different AXF is selected offline', async () => {
    const current = loadDesktopSettings(storage)
    saveDesktopSettings(storage, {
      ...current,
      symbolPath: 'C:\\firmware\\old.axf',
      rttAddress: '0x20000010',
    })
    mocks.pickSymbolFile.mockResolvedValue('D:\\build\\next.axf')

    const result = await useDashboardSetup().loadSymbolFile()

    expect(result).toBe(true)
    expect(mocks.parseAxf).not.toHaveBeenCalled()
    expect(mocks.findRtt).not.toHaveBeenCalled()
    expect(loadDesktopSettings(storage)).toMatchObject({
      symbolPath: 'D:\\build\\next.axf',
      rttAddress: '',
    })
  })

  it('refreshes RTT after reconnecting with an already selected AXF', async () => {
    const current = loadDesktopSettings(storage)
    saveDesktopSettings(storage, {
      ...current,
      symbolPath: 'C:\\firmware\\app.axf',
      rttAddress: '0x20000010',
    })
    mocks.connectDevice.mockImplementationOnce(async () => {
      mocks.deviceStatus.value.axf = {
        loaded: true,
        axf_path: 'C:\\firmware\\app.axf',
      }
      return { connected: true }
    })
    mocks.findRtt.mockResolvedValueOnce({ found: true, addr: '0x20001A40' })

    const result = await useDashboardSetup().quickConnect()

    expect(result).toBe(true)
    expect(mocks.findRtt).toHaveBeenCalledWith('C:\\firmware\\app.axf')
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20001A40')
  })

  it('preserves a manual RTT address when reconnect lookup finds no symbol', async () => {
    const current = loadDesktopSettings(storage)
    saveDesktopSettings(storage, {
      ...current,
      symbolPath: 'C:\\firmware\\app.axf',
      rttAddress: '0x20000010',
    })
    mocks.connectDevice.mockImplementationOnce(async () => {
      mocks.deviceStatus.value.axf = {
        loaded: true,
        axf_path: 'C:\\firmware\\app.axf',
      }
      return { connected: true }
    })
    mocks.findRtt.mockResolvedValueOnce({ found: false, addr: null })

    const result = await useDashboardSetup().quickConnect()

    expect(result).toBe(true)
    expect(mocks.findRtt).toHaveBeenCalledWith('C:\\firmware\\app.axf')
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20000010')
  })

  it('explicit disconnect does not call any serial or Modbus API', async () => {
    mocks.deviceStatus.value.connected = true
    mocks.disconnectDevice.mockResolvedValue(undefined)

    await useDashboardSetup().quickDisconnect()

    expect(mocks.disconnectDevice).toHaveBeenCalledOnce()
  })
})
