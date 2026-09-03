import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import { readonly, ref } from 'vue'

type RuntimeWindow = {
  __TAURI__?: unknown
  __TAURI_INTERNALS__?: unknown
}

export function isTauriRuntime(runtime: RuntimeWindow = window as RuntimeWindow): boolean {
  return Boolean(runtime.__TAURI__ || runtime.__TAURI_INTERNALS__)
}

export function resolveRuntimeBase(configuredBase: string, tauri = isTauriRuntime()): string {
  return tauri ? configuredBase.trim().replace(/\/+$/, '') : ''
}

export const IS_TAURI = isTauriRuntime()
export let API_BASE = resolveRuntimeBase(import.meta.env.VITE_MKLINK_API || '', IS_TAURI)
export let WS_BASE = resolveRuntimeBase(import.meta.env.VITE_MKLINK_WS || '', IS_TAURI)

export function browserRuntimePort(location: Pick<Location, 'port' | 'protocol'> = window.location): number | null {
  const explicit = Number.parseInt(location.port, 10)
  if (Number.isInteger(explicit) && explicit >= 1 && explicit <= 65535) return explicit
  if (location.protocol === 'http:') return 80
  if (location.protocol === 'https:') return 443
  return null
}

const backendPort = ref<number | null>(IS_TAURI ? null : browserRuntimePort())
export const runtimeBackendPort = readonly(backendPort)

export type BackendEndpoint = {
  port: number
  instanceId: string
}

export function applyBackendEndpoint(endpoint: BackendEndpoint): void {
  if (!Number.isInteger(endpoint.port) || endpoint.port < 1 || endpoint.port > 65535) {
    throw new Error('Invalid backend port')
  }
  API_BASE = `http://127.0.0.1:${endpoint.port}`
  WS_BASE = `ws://127.0.0.1:${endpoint.port}`
  backendPort.value = endpoint.port
}

function markBackendUnavailable(): void {
  API_BASE = 'http://127.0.0.1:0'
  WS_BASE = 'ws://127.0.0.1:0'
  backendPort.value = null
}

export async function initializeRuntimeEndpoint(): Promise<void> {
  if (!IS_TAURI) return
  markBackendUnavailable()
  await listen<BackendEndpoint>('backend-endpoint-changed', event => {
    applyBackendEndpoint(event.payload)
  })
  for (let attempt = 0; attempt < 200; attempt++) {
    const endpoint = await invoke<BackendEndpoint | null>('backend_endpoint')
    if (endpoint) {
      applyBackendEndpoint(endpoint)
      return
    }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error('Timed out waiting for the desktop backend endpoint')
}

export async function restartRuntimeBackend(): Promise<BackendEndpoint> {
  const endpoint = await invoke<BackendEndpoint>('restart_sidecar')
  applyBackendEndpoint(endpoint)
  return endpoint
}
