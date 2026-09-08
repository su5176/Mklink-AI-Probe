import { ref, readonly } from 'vue'
import { API_BASE } from '../lib/runtimeEndpoint'
import { tr } from './useLanguage'

async function api(path: string, options?: RequestInit) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    const detail = err?.detail
    if (detail?.code === 'PROBE_BUSY') {
      const owner = String(detail.conflict_owner ?? '').split(':').at(-1)
      const name = ({ rtt: 'RTT View', superwatch: 'SuperWatch', systemview: 'RTOS Trace', vofa: 'VOFA+' } as Record<string, string>)[owner ?? ''] ?? owner
      throw new Error(tr(`请先停止 ${name || '当前采集'}，再启动此功能。`, `Stop ${name || 'the current capture'} before starting this feature.`))
    }
    throw new Error(typeof detail === 'string' ? detail : detail?.message || res.statusText)
  }
  return res.json()
}

type StreamState = 'idle' | 'running' | 'paused' | 'error'

export function useDashboard(type: string) {
  const state = ref<StreamState>('idle')
  const error = ref<string | null>(null)

  async function start(params?: Record<string, unknown>): Promise<boolean> {
    error.value = null
    try {
      const body = params || {}
      await api(`/api/dash/${type}/start`, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      state.value = 'running'
      return true
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      state.value = 'error'
      return false
    }
  }

  async function stop(): Promise<boolean> {
    try {
      await api(`/api/dash/${type}/stop`, { method: 'POST' })
      state.value = 'idle'
      error.value = null
      return true
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      return false
    }
  }

  async function pause() {
    try {
      await api(`/api/dash/${type}/pause`, { method: 'POST' })
      state.value = 'paused'
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function resume() {
    try {
      await api(`/api/dash/${type}/resume`, { method: 'POST' })
      state.value = 'running'
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function getStatus() {
    return api(`/api/dash/${type}/status`)
  }

  async function getHistory() {
    return api(`/api/dash/${type}/history`)
  }

  return {
    state: readonly(state),
    error: readonly(error),
    start,
    stop,
    pause,
    resume,
    getStatus,
    getHistory,
  }
}

// Device memory/register API
export function useDeviceApi() {
  async function readMemory(address: string, size: number) {
    return api('/api/device/read-memory', {
      method: 'POST',
      body: JSON.stringify({ address, size }),
    })
  }

  async function writeMemory(address: string, data_hex: string) {
    return api('/api/device/write-memory', {
      method: 'POST',
      body: JSON.stringify({ address, data_hex }),
    })
  }

  async function readVariable(name: string) {
    return api('/api/device/read-variable', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
  }

  async function writeVariable(name: string, value: number) {
    return api('/api/device/write-variable', {
      method: 'POST',
      body: JSON.stringify({ name, value }),
    })
  }

  async function readRegister(name: string) {
    return api('/api/device/read-register', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
  }

  async function getCoreRegisters() {
    return api('/api/device/core-registers')
  }

  async function getHardfaultDetail() {
    return api('/api/device/hardfault-detail')
  }

  async function getMemoryMap() {
    return api('/api/device/memory-map')
  }

  return {
    readMemory,
    writeMemory,
    readVariable,
    writeVariable,
    readRegister,
    getCoreRegisters,
    getHardfaultDetail,
    getMemoryMap,
  }
}

// Symbols API
export function useSymbolsApi() {
  async function search(q: string) {
    return api(`/api/symbols/search?q=${encodeURIComponent(q)}`)
  }

  async function typeinfo(name: string) {
    return api(`/api/symbols/typeinfo?name=${encodeURIComponent(name)}`)
  }

  return { search, typeinfo }
}
