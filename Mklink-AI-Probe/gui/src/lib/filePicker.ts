import { isTauri } from '@tauri-apps/api/core'

const SYMBOL_FILTER = { name: 'AXF / ELF', extensions: ['axf', 'elf', 'out'] }
const FIRMWARE_FILTER = { name: 'BIN / HEX', extensions: ['bin', 'hex'] }

export type PickedFile = string | File | null

export interface BrowserFirmwareFileHandle {
  readonly kind: 'file'
  readonly name: string
  getFile(): Promise<File>
}

export interface TrackedBrowserFirmwareFile {
  readonly kind: 'tracked-browser-firmware'
  readonly file: File
  readonly handle: BrowserFirmwareFileHandle
}

export type PickedFirmwareSource = string | File | TrackedBrowserFirmwareFile

interface FirmwarePickerWindow extends Window {
  showOpenFilePicker?: (options: {
    multiple: boolean
    excludeAcceptAllOption: boolean
    types: Array<{
      description: string
      accept: Record<string, string[]>
    }>
  }) => Promise<BrowserFirmwareFileHandle[]>
}

function pickBrowserFile(filter: { name: string, extensions: string[] }): Promise<File | null> {
  return new Promise(resolve => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = filter.extensions.map(extension => `.${extension}`).join(',')
    const finish = (file: File | null) => {
      input.remove()
      resolve(file)
    }
    input.addEventListener('change', () => finish(input.files?.[0] ?? null), { once: true })
    input.addEventListener('cancel', () => finish(null), { once: true })
    input.click()
  })
}

async function pickFile(filter: { name: string, extensions: string[] }): Promise<PickedFile> {
  if (!isTauri()) return pickBrowserFile(filter)
  try {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const result = await open({ multiple: false, filters: [filter] })
    return typeof result === 'string' ? result : null
  } catch {
    return null
  }
}

export function pickSymbolFile(): Promise<PickedFile> {
  return pickFile(SYMBOL_FILTER)
}

export async function pickFirmwareFiles(multiple = false): Promise<Array<string | File>> {
  if (!isTauri()) {
    const selected = await pickBrowserFile(FIRMWARE_FILTER)
    return selected ? [selected] : []
  }
  try {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const result: unknown = await open({ multiple, filters: [FIRMWARE_FILTER] })
    if (typeof result === 'string') return [result]
    return Array.isArray(result) ? result.filter((item: unknown): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
}

export function supportsTrackedFirmwarePicker(): boolean {
  return !isTauri() && typeof (window as FirmwarePickerWindow).showOpenFilePicker === 'function'
}

export async function pickTrackedFirmwareFiles(multiple = false): Promise<PickedFirmwareSource[]> {
  if (isTauri()) return pickFirmwareFiles(multiple)

  const picker = (window as FirmwarePickerWindow).showOpenFilePicker
  if (typeof picker !== 'function') {
    const selected = await pickBrowserFile(FIRMWARE_FILTER)
    return selected ? [selected] : []
  }

  try {
    const handles = await picker({
      multiple,
      excludeAcceptAllOption: true,
      types: [{
        description: FIRMWARE_FILTER.name,
        accept: { 'application/octet-stream': ['.bin', '.hex'] },
      }],
    })
    return Promise.all(handles.map(async handle => ({
      kind: 'tracked-browser-firmware' as const,
      file: await handle.getFile(),
      handle,
    })))
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return []
    const selected = await pickBrowserFile(FIRMWARE_FILTER)
    return selected ? [selected] : []
  }
}

export function isTrackedBrowserFirmwareFile(
  source: PickedFirmwareSource,
): source is TrackedBrowserFirmwareFile {
  return typeof source === 'object'
    && source !== null
    && !(source instanceof File)
    && source.kind === 'tracked-browser-firmware'
}

export async function listenForFirmwarePathDrops(
  onDrop: (paths: string[]) => void,
  onHover?: (active: boolean) => void,
): Promise<() => void> {
  if (!isTauri()) return () => undefined
  const { getCurrentWebview } = await import('@tauri-apps/api/webview')
  return getCurrentWebview().onDragDropEvent(event => {
    if (event.payload.type === 'drop') {
      onHover?.(false)
      onDrop(event.payload.paths)
    } else if (event.payload.type === 'over') {
      onHover?.(true)
    } else {
      onHover?.(false)
    }
  })
}
