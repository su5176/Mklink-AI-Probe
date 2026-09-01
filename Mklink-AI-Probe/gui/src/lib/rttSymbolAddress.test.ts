import { describe, expect, it, vi } from 'vitest'
import {
  loadDesktopSettings,
  saveDesktopSettings,
  type DesktopSettings,
} from './desktopSettings'
import { cancelRttAddressRefresh, refreshRttAddressForSymbol } from './rttSymbolAddress'
import type { RttFindResponse } from '../types/mklink'

class MemoryStorage {
  private readonly values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }
}

function settings(overrides: Partial<DesktopSettings> = {}): DesktopSettings {
  return {
    version: 1,
    symbolPath: 'C:\\firmware\\app.axf',
    symbolDisplayPath: '',
    rttAddress: '0x20000010',
    rttEncoding: 'utf-8',
    transmitMode: 'text',
    lineEnding: '',
    sendHistory: [],
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

describe('RTT symbol address refresh', () => {
  it('keeps the current address during lookup and persists the detected address', async () => {
    const storage = new MemoryStorage()
    saveDesktopSettings(storage, settings())
    const pending = deferred<RttFindResponse>()
    const findRtt = vi.fn(() => pending.promise)

    const refresh = refreshRttAddressForSymbol(storage, 'C:\\firmware\\app.axf', findRtt)

    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20000010')
    pending.resolve({ found: true, addr: '0x20001A40', source: 'binary:app.axf' })
    const result = await refresh

    expect(findRtt).toHaveBeenCalledWith('C:\\firmware\\app.axf')
    expect(result).toMatchObject({ address: '0x20001A40', stale: false })
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20001A40')
  })

  it('preserves a manual address when RTT is absent or lookup fails', async () => {
    const storage = new MemoryStorage()
    saveDesktopSettings(storage, settings())

    const missing = await refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      vi.fn().mockResolvedValue({ found: false, addr: null }),
    )
    expect(missing).toMatchObject({ address: '0x20000010', stale: false })
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20000010')

    saveDesktopSettings(storage, settings())
    const failed = await refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      vi.fn().mockRejectedValue(new Error('parser unavailable')),
    )
    expect(failed.error).toEqual(new Error('parser unavailable'))
    expect(failed).toMatchObject({ address: '0x20000010', stale: false })
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20000010')
  })

  it('reloads the latest settings after a lookup failure', async () => {
    const storage = new MemoryStorage()
    saveDesktopSettings(storage, settings())
    const pending = deferred<RttFindResponse>()
    const refresh = refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      () => pending.promise,
    )
    const history = [{
      text: 'help',
      mode: 'text' as const,
      lineEnding: '\r' as const,
      timestamp: 123,
    }]
    saveDesktopSettings(storage, settings({ sendHistory: history }))

    pending.reject(new Error('parser unavailable'))
    const result = await refresh

    expect(result.settings.sendHistory).toEqual(history)
    expect(loadDesktopSettings(storage).sendHistory).toEqual(history)
  })

  it('does not let an older concurrent lookup overwrite the newest result', async () => {
    const storage = new MemoryStorage()
    saveDesktopSettings(storage, settings())
    const first = deferred<RttFindResponse>()
    const second = deferred<RttFindResponse>()

    const oldRefresh = refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      () => first.promise,
    )
    const newRefresh = refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      () => second.promise,
    )
    second.resolve({ found: true, addr: '0x20002000' })
    expect((await newRefresh).stale).toBe(false)
    first.resolve({ found: true, addr: '0x20001000' })

    expect(await oldRefresh).toMatchObject({
      address: '0x20002000',
      stale: true,
    })
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20002000')
  })

  it('does not overwrite an address edited while automatic lookup is pending', async () => {
    const storage = new MemoryStorage()
    saveDesktopSettings(storage, settings())
    const pending = deferred<RttFindResponse>()

    const refresh = refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      () => pending.promise,
    )
    saveDesktopSettings(storage, settings({ rttAddress: '0x20003333' }))
    pending.resolve({ found: true, addr: '0x20001A40' })

    expect(await refresh).toMatchObject({
      address: '0x20003333',
      stale: true,
    })
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20003333')
  })

  it('can cancel automatic lookup as soon as manual editing begins', async () => {
    const storage = new MemoryStorage()
    saveDesktopSettings(storage, settings())
    const pending = deferred<RttFindResponse>()

    const refresh = refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      () => pending.promise,
    )
    cancelRttAddressRefresh(storage)
    pending.resolve({ found: true, addr: '0x20001A40' })

    expect(await refresh).toMatchObject({
      address: '0x20000010',
      stale: true,
    })
    expect(loadDesktopSettings(storage).rttAddress).toBe('0x20000010')
  })

  it('does not apply a late result after another symbol file is selected', async () => {
    const storage = new MemoryStorage()
    saveDesktopSettings(storage, settings())
    const pending = deferred<RttFindResponse>()

    const refresh = refreshRttAddressForSymbol(
      storage,
      'C:\\firmware\\app.axf',
      () => pending.promise,
    )
    saveDesktopSettings(storage, settings({
      symbolPath: 'D:\\build\\next.elf',
      rttAddress: '0x20003000',
    }))
    pending.resolve({ found: true, addr: '0x20001A40' })

    const result = await refresh
    expect(result.stale).toBe(true)
    expect(loadDesktopSettings(storage)).toMatchObject({
      symbolPath: 'D:\\build\\next.elf',
      rttAddress: '0x20003000',
    })
  })
})
