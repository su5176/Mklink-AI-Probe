import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadTextFile, saveBlobFile, timestampedLogName } from './downloadTextFile'

describe('downloadTextFile', () => {
  afterEach(() => {
    delete (window as Window & { showSaveFilePicker?: unknown }).showSaveFilePicker
    vi.doUnmock('@tauri-apps/api/core')
    vi.doUnmock('@tauri-apps/plugin-dialog')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('downloads a UTF-8 text file and releases the object URL', () => {
    vi.useFakeTimers()
    const createObjectURL = vi.fn().mockReturnValue('blob:test')
    const revokeObjectURL = vi.fn()
    const click = vi.fn()
    const remove = vi.fn()
    const appendChild = vi.spyOn(document.body, 'appendChild').mockImplementation(node => node)
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    vi.spyOn(document, 'createElement').mockReturnValue({
      href: '', download: '', style: {}, click, remove,
    } as unknown as HTMLAnchorElement)

    downloadTextFile('serial.log', '温度=25')

    const blob = createObjectURL.mock.calls[0][0] as Blob
    expect(blob.type).toBe('text/plain;charset=utf-8')
    expect(appendChild).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
    expect(remove).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).not.toHaveBeenCalled()
    vi.runAllTimers()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:test')
  })

  it('creates filesystem-safe timestamped log names', () => {
    expect(timestampedLogName('rtt', new Date('2026-08-10T01:02:03.456Z')))
      .toBe('rtt-2026-08-10T01-02-03-456Z.log')
  })

  it('uses the browser file picker when the user chooses a save location', async () => {
    const write = vi.fn().mockResolvedValue(undefined)
    const close = vi.fn().mockResolvedValue(undefined)
    const picker = vi.fn().mockResolvedValue({
      createWritable: vi.fn().mockResolvedValue({ write, close }),
    })
    Object.defineProperty(window, 'showSaveFilePicker', {
      value: picker,
      configurable: true,
    })
    const blob = new Blob(['uf2'], { type: 'application/octet-stream' })

    await expect(saveBlobFile('MicroLink_V3.3.7.uf2', blob)).resolves.toBe(true)

    expect(picker).toHaveBeenCalledWith(expect.objectContaining({
      suggestedName: 'MicroLink_V3.3.7.uf2',
    }))
    expect(write).toHaveBeenCalledWith(blob)
    expect(close).toHaveBeenCalledOnce()
  })

  it('uses a matching native extension and writes the selected desktop file', async () => {
    const save = vi.fn().mockResolvedValue('C:\\captures\\superwatch.csv')
    const invoke = vi.fn().mockResolvedValue(undefined)
    vi.doMock('@tauri-apps/api/core', () => ({ isTauri: () => true, invoke }))
    vi.doMock('@tauri-apps/plugin-dialog', () => ({ save }))
    const { saveTextFile } = await import('./downloadTextFile')

    await expect(saveTextFile('superwatch.csv', 'time,value\n0,1')).resolves.toBe(true)

    expect(save).toHaveBeenCalledWith({
      defaultPath: 'superwatch.csv',
      filters: [{ name: 'MKLink Data', extensions: ['csv'] }],
    })
    expect(invoke).toHaveBeenCalledWith('write_file', {
      path: 'C:\\captures\\superwatch.csv',
      contents: Array.from(new TextEncoder().encode('time,value\n0,1')),
    })
  })
})
