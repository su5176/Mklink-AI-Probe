import { describe, expect, it } from 'vitest'
import {
  DESKTOP_SETTINGS_STORAGE_KEY,
  loadDesktopSettings,
  isSameFileSourcePath,
  isSymbolFilePath,
  recordSuccessfulSend,
  saveDesktopSettings,
  type DesktopSettings,
} from './desktopSettings'

class MemoryStorage {
  readonly values = new Map<string, string>()

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
    symbolPath: '',
    symbolDisplayPath: '',
    rttAddress: '',
    rttEncoding: 'utf-8',
    transmitMode: 'text',
    lineEnding: '',
    sendHistory: [],
    ...overrides,
  }
}

describe('desktop settings', () => {
  it('validates supported symbol path extensions after trimming', () => {
    expect(isSymbolFilePath(' C:\\firmware\\app.axf ')).toBe(true)
    expect(isSymbolFilePath('app.ELF')).toBe(true)
    expect(isSymbolFilePath('app.out')).toBe(true)
    expect(isSymbolFilePath('app.map')).toBe(false)
    expect(isSymbolFilePath('')).toBe(false)
  })

  it('compares Windows file sources case-insensitively without weakening POSIX paths', () => {
    expect(isSameFileSourcePath('C:\\Build\\App.axf', 'c:/build/app.axf')).toBe(true)
    expect(isSameFileSourcePath('/tmp/App.axf', '/tmp/app.axf')).toBe(false)
    expect(isSameFileSourcePath('', 'C:\\build\\app.axf')).toBe(false)
  })

  it('returns fresh defaults when no saved settings exist', () => {
    const storage = new MemoryStorage()

    const first = loadDesktopSettings(storage)
    first.sendHistory.push({ text: 'mutated', mode: 'text', lineEnding: '', timestamp: 1 })

    expect(loadDesktopSettings(storage)).toEqual(settings())
  })

  it('recovers from malformed JSON and unsupported versions', () => {
    const storage = new MemoryStorage()
    storage.values.set(DESKTOP_SETTINGS_STORAGE_KEY, '{not-json')
    expect(loadDesktopSettings(storage)).toEqual(settings())

    storage.values.set(DESKTOP_SETTINGS_STORAGE_KEY, JSON.stringify({ version: 2, symbolPath: 'old.axf' }))
    expect(loadDesktopSettings(storage)).toEqual(settings())
  })

  it('saves and loads validated settings through the versioned key', () => {
    const storage = new MemoryStorage()
    const original = settings({
      symbolPath: 'C:\\firmware\\app.axf',
      symbolDisplayPath: 'app.axf',
      rttAddress: '0x20001A40',
      rttEncoding: 'gb18030',
      transmitMode: 'hex',
      lineEnding: '\r\n',
      sendHistory: [{ text: 'AA 55', mode: 'hex', lineEnding: '', timestamp: 10 }],
    })

    const saved = saveDesktopSettings(storage, original)
    expect(saved).not.toBe(original)
    expect(saved.sendHistory[0]).not.toBe(original.sendHistory[0])
    original.sendHistory[0].text = 'changed after save'

    expect(storage.values.has('mklink.desktop.settings.v1')).toBe(true)
    expect(loadDesktopSettings(storage)).toEqual(settings({
      symbolPath: 'C:\\firmware\\app.axf',
      symbolDisplayPath: 'app.axf',
      rttAddress: '0x20001A40',
      rttEncoding: 'gb18030',
      transmitMode: 'hex',
      lineEnding: '\r\n',
      sendHistory: [{ text: 'AA 55', mode: 'hex', lineEnding: '', timestamp: 10 }],
    }))
  })

  it('sanitizes invalid fields, drops retired MAP settings, and keeps valid history', () => {
    const storage = new MemoryStorage()
    storage.values.set(DESKTOP_SETTINGS_STORAGE_KEY, JSON.stringify({
      version: 1,
      symbolPath: 42,
      mapPath: 'valid.map',
      rttAddress: '20001A40',
      rttEncoding: 'shift-jis',
      transmitMode: 'binary',
      lineEnding: '\r\r',
      sendHistory: [
        { text: 'valid', mode: 'text', lineEnding: '\n', timestamp: 2 },
        { text: 3, mode: 'text', lineEnding: '', timestamp: 1 },
      ],
    }))

    expect(loadDesktopSettings(storage)).toEqual(settings({
      sendHistory: [{ text: 'valid', mode: 'text', lineEnding: '\n', timestamp: 2 }],
    }))
  })

  it('migrates legacy MAP fields without losing the remaining version-one settings', () => {
    const storage = new MemoryStorage()
    storage.values.set(DESKTOP_SETTINGS_STORAGE_KEY, JSON.stringify({
      version: 1,
      symbolPath: 'C:\\firmware\\app.axf',
      symbolDisplayPath: 'app.axf',
      mapPath: 'C:\\firmware\\app.map',
      mapDisplayPath: 'app.map',
      rttAddress: '0x20001A40',
      rttEncoding: 'gb18030',
      transmitMode: 'hex',
      lineEnding: '\r\n',
      sendHistory: [{ text: 'AA 55', mode: 'hex', lineEnding: '', timestamp: 10 }],
    }))

    const migrated = loadDesktopSettings(storage)
    expect(migrated).toEqual(settings({
      symbolPath: 'C:\\firmware\\app.axf',
      symbolDisplayPath: 'app.axf',
      rttAddress: '0x20001A40',
      rttEncoding: 'gb18030',
      transmitMode: 'hex',
      lineEnding: '\r\n',
      sendHistory: [{ text: 'AA 55', mode: 'hex', lineEnding: '', timestamp: 10 }],
    }))

    saveDesktopSettings(storage, migrated)
    const persisted = JSON.parse(storage.values.get(DESKTOP_SETTINGS_STORAGE_KEY) ?? '{}')
    expect(persisted).not.toHaveProperty('mapPath')
    expect(persisted).not.toHaveProperty('mapDisplayPath')
  })

  it('keeps only twenty newest successful sends and collapses consecutive duplicates', () => {
    const storage = new MemoryStorage()

    recordSuccessfulSend(storage, { text: 'same', mode: 'text', lineEnding: '\r\n' })
    recordSuccessfulSend(storage, { text: 'same', mode: 'text', lineEnding: '\r\n' })
    expect(loadDesktopSettings(storage).sendHistory.map(entry => entry.text)).toEqual(['same'])

    for (let index = 0; index < 21; index += 1) {
      recordSuccessfulSend(storage, { text: `command-${index}`, mode: 'hex', lineEnding: '' })
    }

    const history = loadDesktopSettings(storage).sendHistory
    expect(history).toHaveLength(20)
    expect(history.map(entry => entry.text)).toEqual(
      Array.from({ length: 20 }, (_, index) => `command-${20 - index}`),
    )
    expect(history.every(entry => Number.isFinite(entry.timestamp))).toBe(true)
  })
})
