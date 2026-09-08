import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fileContentStamp } from './fileContent'
vi.mock('./fileContent', () => ({ fileContentStamp: (file: File) => file.text() }))
import { loadDesktopSettings, saveDesktopSettings } from './desktopSettings'
const handles = vi.hoisted(() => new WeakMap<File, { getFile(): Promise<File> }>())
vi.mock('./filePicker', () => ({ symbolFileHandle: (file: File) => handles.get(file) }))
import { trackSymbolSource, trackedSymbolError } from './trackedSymbolSource'

beforeEach(() => { vi.useFakeTimers(); const values = new Map<string, string>(); vi.stubGlobal('localStorage', { getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => values.set(k, v) }) })
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('browser symbol tracking', () => {
  async function setup() {
    const original = new File(['old'], 'app.axf', { lastModified: 1 })
    const rebuilt = new File(['new'], 'app.axf', { lastModified: 1 })
    const getFile = vi.fn().mockResolvedValue(rebuilt)
    handles.set(original, { getFile })
    const source = { path: '/cache/old.axf', name: 'app.axf', size: 3, sha256: await fileContentStamp(original) }
    const upload = vi.fn().mockResolvedValue({ ...source, path: '/cache/new.axf', sha256: await fileContentStamp(rebuilt) })
    const actions = { upload, connected: () => true, parse: vi.fn().mockResolvedValue({ loaded: true }), refreshRtt: vi.fn().mockResolvedValue({}) }
    saveDesktopSettings(window.localStorage, { ...loadDesktopSettings(window.localStorage), symbolPath: source.path })
    trackSymbolSource(original, source, actions)
    return { actions, getFile }
  }

  it('reloads changed bytes despite identical file name, size and timestamp', async () => {
    const { actions } = await setup()
    await vi.advanceTimersByTimeAsync(1100)
    expect(actions.upload).toHaveBeenCalledOnce()
    expect(actions.parse).toHaveBeenCalledWith('/cache/new.axf')
    expect(actions.refreshRtt).toHaveBeenCalledWith('/cache/new.axf')
    expect(loadDesktopSettings(window.localStorage).symbolPath).toBe('/cache/new.axf')
    await vi.advanceTimersByTimeAsync(2000)
    expect(actions.upload).toHaveBeenCalledOnce()
  })

  it('does not replace a different source chosen while an upload is pending', async () => {
    const { actions } = await setup()
    actions.upload.mockImplementationOnce(async () => {
      saveDesktopSettings(window.localStorage, { ...loadDesktopSettings(window.localStorage), symbolPath: '/chosen.axf' })
      return { path: '/late.axf', name: 'app.axf', size: 3, sha256: 'late' }
    })
    await vi.advanceTimersByTimeAsync(1100)
    expect(loadDesktopSettings(window.localStorage).symbolPath).toBe('/chosen.axf')
    expect(actions.parse).not.toHaveBeenCalled()
  })

  it('reports parse failure without repeatedly stopping and reparsing unchanged bytes', async () => {
    const { actions } = await setup()
    actions.parse.mockRejectedValue(new Error('invalid AXF'))
    await vi.advanceTimersByTimeAsync(3100)
    expect(actions.parse).toHaveBeenCalledOnce()
    expect(trackedSymbolError.value).toBe('invalid AXF')
  })
})
