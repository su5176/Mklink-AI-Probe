import { isTauri } from '@tauri-apps/api/core'

export function timestampedLogName(prefix: string, now = new Date()): string {
  return `${prefix}-${now.toISOString().replace(/[:.]/g, '-')}.log`
}

export function downloadTextFile(filename: string, text: string): void {
  if (isTauri()) {
    void saveTextFile(filename, text)
    return
  }
  downloadBlobFile(filename, new Blob([text], { type: 'text/plain;charset=utf-8' }))
}

export function downloadBlobFile(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.style.display = 'none'
  document.body.appendChild(link)
  try {
    link.click()
  } finally {
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}

export async function saveBlobFile(filename: string, blob: Blob): Promise<boolean> {
  if (!isTauri()) {
    const picker = (window as Window & {
      showSaveFilePicker?: (options?: unknown) => Promise<{
        createWritable: () => Promise<{
          write: (value: Blob) => Promise<void>
          close: () => Promise<void>
        }>
      }>
    }).showSaveFilePicker
    if (picker) {
      try {
        const extension = filename.match(/\.([A-Za-z0-9]+)$/)?.[1]?.toLowerCase() || 'bin'
        const handle = await picker({
          suggestedName: filename,
          types: [{
            description: 'MKLink Data',
            accept: { 'application/octet-stream': [`.${extension}`] },
          }],
        })
        const writable = await handle.createWritable()
        await writable.write(blob)
        await writable.close()
        return true
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return false
        throw error
      }
    }
    downloadBlobFile(filename, blob)
    return true
  }
  const { save } = await import('@tauri-apps/plugin-dialog')
  const extension = filename.match(/\.([A-Za-z0-9]+)$/)?.[1]?.toLowerCase() || 'txt'
  const path = await save({
    defaultPath: filename,
    filters: [{ name: 'MKLink Data', extensions: [extension] }],
  })
  if (!path) return false
  const { invoke } = await import('@tauri-apps/api/core')
  const contents = Array.from(new Uint8Array(await blob.arrayBuffer()))
  await invoke('write_file', { path, contents })
  return true
}

export function saveTextFile(filename: string, text: string): Promise<boolean> {
  return saveBlobFile(filename, new Blob([text], { type: 'text/plain;charset=utf-8' }))
}
