import { ref, onUnmounted } from 'vue'
import {
  API_BASE,
  IS_TAURI,
  restartRuntimeBackend,
  runtimeBackendPort,
} from '../lib/runtimeEndpoint'

/** 'starting' = backend not yet checked / currently booting */
const backendState = ref<'starting' | 'alive' | 'dead'>('starting')
let pollTimer: ReturnType<typeof setInterval> | null = null
let refCount = 0
let firstCheckDone = false

async function checkBackendHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/api/health`, { signal: AbortSignal.timeout(3000) })
    return res.ok
  } catch {
    return false
  }
}

async function refreshHealth() {
  const alive = IS_TAURI
    ? await checkViaTauri()
    : await checkBackendHealth()

  if (alive) {
    backendState.value = 'alive'
    firstCheckDone = true
  } else if (firstCheckDone) {
    // Only show 'dead' after at least one successful check
    // This prevents flashing red during initial startup
    backendState.value = 'dead'
  }
  // If !firstCheckDone && !alive, keep 'starting'
}

async function checkViaTauri(): Promise<boolean> {
  try {
    const alive = await (window as any).__TAURI__.invoke('backend_alive')
    return !!alive
  } catch {
    return await checkBackendHealth()
  }
}

function startHealthPolling(intervalMs = 5000) {
  if (pollTimer) return
  // Fast polling during startup (every 1s), then settle to intervalMs
  let fastPolls = 0
  const maxFastPolls = 15 // 15 × 1s = 15s of fast polling

  refreshHealth()
  pollTimer = setInterval(async () => {
    fastPolls++
    await refreshHealth()
    if (fastPolls >= maxFastPolls && backendState.value === 'starting') {
      backendState.value = 'dead'
      firstCheckDone = true
    }
    // Switch to slower interval after first success or after max fast polls
    if ((backendState.value === 'alive' || fastPolls >= maxFastPolls) && pollTimer) {
      clearInterval(pollTimer)
      pollTimer = setInterval(refreshHealth, intervalMs)
    }
  }, 1000)
}

function stopHealthPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function restart(): Promise<void> {
  backendState.value = 'starting'
  firstCheckDone = false
  if (IS_TAURI) {
    try {
      await restartRuntimeBackend()
    } catch (e) {
      console.error('[useBackendHealth] restart failed:', e)
    }
  }
  // Wait for backend to come back up (up to 15s)
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 500))
    if (await checkBackendHealth()) {
      backendState.value = 'alive'
      firstCheckDone = true
      break
    }
  }
  if (backendState.value === 'starting') {
    backendState.value = 'dead'
    firstCheckDone = true
  }
}

export function useBackendHealth() {
  refCount++
  onUnmounted(() => {
    refCount--
    if (refCount <= 0) {
      stopHealthPolling()
      refCount = 0
    }
  })

  return {
    backendState,
    backendPort: runtimeBackendPort,
    isTauri: IS_TAURI,
    startHealthPolling,
    stopHealthPolling,
    restart,
    refreshHealth,
  }
}
