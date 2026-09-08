// A File System Access handle returns fresh bytes even if a shared-folder sync
// preserves the original timestamp and length. Plain uploads remain snapshots.
export async function fileContentStamp(file: File): Promise<string> {
  const bytes = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('')
}
