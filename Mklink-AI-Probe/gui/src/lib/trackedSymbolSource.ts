import { ref } from 'vue'
import { fileContentStamp } from './fileContent'
import { symbolFileHandle } from './filePicker'
import { isSameFileSourcePath, loadDesktopSettings, saveDesktopSettings } from './desktopSettings'
import type { UploadedFileSource } from '../types/mklink'

export const trackedSymbolPath = ref('')
export const trackedSymbolError = ref('')
let cancel: (() => void) | undefined

interface SymbolTrackingActions {
  upload(file: File): Promise<UploadedFileSource>
  connected(): boolean
  parse(path: string): Promise<unknown>
  refreshRtt(path: string): Promise<unknown>
}

/** The handle belongs to this browser session; plain uploads cannot be watched. */
export function trackSymbolSource(file: File, source: UploadedFileSource, actions: SymbolTrackingActions): void {
  cancel?.()
  trackedSymbolPath.value = ''
  trackedSymbolError.value = ''
  const handle = symbolFileHandle(file)
  if (!handle) return
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let path = source.path
  let stamp = source.sha256
  const current = () => !stopped && isSameFileSourcePath(loadDesktopSettings(window.localStorage).symbolPath, path)
  cancel = () => { stopped = true; if (timer !== undefined) clearTimeout(timer) }
  trackedSymbolPath.value = path
  const poll = async () => {
    if (!current()) { if (!stopped) trackedSymbolPath.value = ''; return }
    try {
      const next = await handle.getFile()
      const nextStamp = await fileContentStamp(next)
      if (!current()) return
      if (nextStamp !== stamp) {
        const uploaded = await actions.upload(next)
        if (!current()) return
        const settings = loadDesktopSettings(window.localStorage)
        path = uploaded.path
        saveDesktopSettings(window.localStorage, { ...settings, symbolPath: path,
          symbolDisplayPath: uploaded.name || next.name, rttAddress: '' })
        trackedSymbolPath.value = path
        stamp = nextStamp
        if (actions.connected()) {
          await actions.parse(path)
          if (!current()) return
          await actions.refreshRtt(path)
        }
        trackedSymbolError.value = ''
      }
    } catch (error) {
      if (current()) trackedSymbolError.value = error instanceof Error ? error.message : String(error)
    } finally {
      if (current()) timer = setTimeout(poll, 1000)
    }
  }
  timer = setTimeout(poll, 1000)
}
