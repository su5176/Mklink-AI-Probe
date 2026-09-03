import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.doUnmock('@tauri-apps/plugin-dialog')
  vi.doUnmock('@tauri-apps/api/core')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.resetModules()
})

async function loadPickerWithDialog(result: unknown) {
  vi.doMock('@tauri-apps/api/core', () => ({ isTauri: () => true }))
  const open = vi.fn().mockResolvedValue(result)
  vi.doMock('@tauri-apps/plugin-dialog', () => ({ open }))
  return { open, picker: await import('./filePicker') }
}

describe('file picker', () => {
  it('opens an AXF/ELF single-file dialog', async () => {
    const { open, picker } = await loadPickerWithDialog('C:\\firmware\\app.axf')

    await expect(picker.pickSymbolFile()).resolves.toBe('C:\\firmware\\app.axf')
    expect(open).toHaveBeenCalledWith({
      multiple: false,
      filters: [{ name: 'AXF / ELF', extensions: ['axf', 'elf', 'out'] }],
    })
  })

  it('returns null when the dialog is cancelled', async () => {
    const { picker } = await loadPickerWithDialog(null)

    await expect(picker.pickSymbolFile()).resolves.toBeNull()
  })

  it('returns null when the Tauri dialog plugin is unavailable', async () => {
    vi.doMock('@tauri-apps/api/core', () => ({ isTauri: () => true }))
    vi.doMock('@tauri-apps/plugin-dialog', () => {
      throw new Error('dialog plugin unavailable')
    })
    const picker = await import('./filePicker')

    await expect(picker.pickSymbolFile()).resolves.toBeNull()
  })

  it('opens a native browser file input when Tauri is unavailable', async () => {
    vi.doMock('@tauri-apps/api/core', () => ({ isTauri: () => false }))
    const selected = new File(['ELF'], 'firmware.axf', { type: 'application/octet-stream' })
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function () {
      expect(this.accept).toBe('.axf,.elf,.out')
      Object.defineProperty(this, 'files', { configurable: true, value: [selected] })
      this.dispatchEvent(new Event('change'))
    })
    const picker = await import('./filePicker')

    await expect(picker.pickSymbolFile()).resolves.toBe(selected)
    expect(click).toHaveBeenCalledOnce()
  })

  it('returns a tracked browser firmware handle when the File System Access API is available', async () => {
    vi.doMock('@tauri-apps/api/core', () => ({ isTauri: () => false }))
    const selected = new File(['firmware'], 'demo.bin', { lastModified: 123 })
    const handle = {
      kind: 'file' as const,
      name: selected.name,
      getFile: vi.fn().mockResolvedValue(selected),
    }
    const showOpenFilePicker = vi.fn().mockResolvedValue([handle])
    vi.stubGlobal('showOpenFilePicker', showOpenFilePicker)
    const picker = await import('./filePicker')

    await expect(picker.pickTrackedFirmwareFiles()).resolves.toEqual([{
      kind: 'tracked-browser-firmware',
      file: selected,
      handle,
    }])
    expect(picker.supportsTrackedFirmwarePicker()).toBe(true)
    expect(showOpenFilePicker).toHaveBeenCalledWith({
      multiple: false,
      excludeAcceptAllOption: true,
      types: [{
        description: 'BIN / HEX',
        accept: { 'application/octet-stream': ['.bin', '.hex'] },
      }],
    })
  })
})
