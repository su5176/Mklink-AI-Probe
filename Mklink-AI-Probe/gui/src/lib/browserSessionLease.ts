import { IS_TAURI } from './runtimeEndpoint'

const RETRY_DELAY_MS = 1000

export function browserSessionSocketUrl(
  location: Pick<Location, 'host' | 'protocol'> = window.location,
  clientId: string,
): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}/ws/browser-session?client_id=${encodeURIComponent(clientId)}`
}

function createClientId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `mklink-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function releaseBrowserDevice(clientId: string): void {
  const body = new Blob([JSON.stringify({ client_id: clientId })], {
    type: 'application/json',
  })
  // The backend releases the shared Device when the last browser session is
  // released. Keep this as one request so disconnect and session accounting
  // cannot race two concurrent Device.close() calls during pagehide.
  if (navigator.sendBeacon?.('/api/browser-session/release', body)) return
  // sendBeacon can be unavailable (or return false when its queue is full).
  // keepalive gives Chromium/WebView2 a second reliable page-close path.
  void fetch('/api/browser-session/release', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
    keepalive: true,
  }).catch(() => undefined)
}

export function startBrowserSessionLease(enabled = !IS_TAURI): () => void {
  if (!enabled) return () => undefined

  const clientId = createClientId()
  let socket: WebSocket | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let stopped = false

  const connect = () => {
    if (stopped) return
    socket = new WebSocket(browserSessionSocketUrl(window.location, clientId))
    socket.addEventListener('close', event => {
      socket = null
      if (stopped || event.code === 1008) return
      retryTimer = setTimeout(connect, RETRY_DELAY_MS)
    })
  }

  const stop = () => {
    if (stopped) return
    stopped = true
    window.removeEventListener('pagehide', stop)
    if (retryTimer !== null) clearTimeout(retryTimer)
    socket?.close(1000, 'browser page closed')
    socket = null
    releaseBrowserDevice(clientId)
  }

  window.addEventListener('pagehide', stop)
  connect()
  return stop
}
