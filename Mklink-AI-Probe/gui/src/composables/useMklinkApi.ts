import { ref, readonly } from 'vue'
import type {
  PortInfo,
  McuProfile,
  ProjectConfig,
  ProjectInfo,
  RttConfig,
  ConfigStatus,
  DeviceStatus,
  MicrokeenInfo,
  ConnectRequest,
  FlashRequest,
  ProjectHistory,
  ProbeFirmwareCheck,
  RttFindResponse,
  RttWriteResponse,
  FileSourceKind,
  UploadedFileSource,
} from '../types/mklink'
import { toHexPayload } from '../lib/rttTransmit'
import type { RttEncoding } from '../lib/desktopSettings'
import { API_BASE } from '../lib/runtimeEndpoint'

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers)
  if (!(options?.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    let msg = res.statusText
    if (err) {
      const d = err.detail
      if (typeof d === 'string') msg = d
      else if (Array.isArray(d)) msg = d.map((e: any) => e.msg || String(e)).join('; ')
      else if (d && typeof d === 'object' && 'message' in d) msg = String(d.message)
      else if (d) msg = JSON.stringify(d)
    }
    throw new Error(msg)
  }
  return res.json()
}

const deviceStatus = ref<DeviceStatus>({
  connected: false,
  state: 'disconnected',
  mcu: null,
  idcode: null,
  port: null,
  axf: { loaded: false },
})

let statusInterval: ReturnType<typeof setInterval> | null = null

export function useMklinkApi() {
  async function listPorts(): Promise<PortInfo[]> {
    return api('/api/ports')
  }

  async function discoverPort(): Promise<{ port: string | null }> {
    return api('/api/ports/discover')
  }

  async function getProfiles(): Promise<McuProfile[]> {
    return api('/api/profiles')
  }

  async function getConfig(): Promise<ProjectConfig> {
    return api('/api/config')
  }

  async function updateConfig(config: ProjectConfig): Promise<ProjectConfig> {
    return api('/api/config', {
      method: 'PUT',
      body: JSON.stringify(config),
    })
  }

  async function uploadFileSource(
    kind: FileSourceKind,
    file: File,
  ): Promise<UploadedFileSource> {
    const body = new FormData()
    body.append('file', file)
    return api(`/api/files/${kind}`, { method: 'POST', body })
  }

  async function getConfigStatus(): Promise<ConfigStatus> {
    return api('/api/config/status')
  }

  async function getProjectInfo(): Promise<ProjectInfo> {
    return api('/api/project')
  }

  async function getRttConfig(): Promise<RttConfig> {
    return api('/api/rtt-config')
  }

  async function updateRttConfig(config: RttConfig): Promise<RttConfig> {
    return api('/api/rtt-config', {
      method: 'PUT',
      body: JSON.stringify(config),
    })
  }

  async function getMicrokeenInfo(): Promise<MicrokeenInfo> {
    return api('/api/microkeen')
  }

  async function probeFirmwareCheck(): Promise<ProbeFirmwareCheck> {
    return api<ProbeFirmwareCheck>('/api/probe/firmware-check')
  }

  async function connectDevice(req: ConnectRequest): Promise<DeviceStatus> {
    const result = await api<DeviceStatus>('/api/device/connect', {
      method: 'POST',
      body: JSON.stringify(req),
    })
    await refreshStatus()
    return result
  }

  async function disconnectDevice(): Promise<void> {
    await api('/api/device/disconnect', { method: 'POST' })
    await refreshStatus()
  }

  async function refreshStatus(): Promise<DeviceStatus> {
    try {
      const s = await api<DeviceStatus>('/api/device/status')
      deviceStatus.value = s
      return s
    } catch {
      deviceStatus.value = {
        connected: false,
        state: 'disconnected',
        mcu: null,
        idcode: null,
        port: null,
        axf: { loaded: false },
      }
      return deviceStatus.value
    }
  }

  async function parseAxf(axf?: string, elfBackend?: 'builtin' | 'external') {
    const result = await api('/api/device/parse-axf', {
      method: 'POST',
      body: JSON.stringify({
        ...(axf ? { axf } : {}),
        ...(elfBackend ? { elf_backend: elfBackend } : {}),
      }),
    })
    await refreshStatus()
    return result
  }

  async function flashDevice(req: FlashRequest) {
    return api('/api/device/flash', {
      method: 'POST',
      body: JSON.stringify(req),
    })
  }

  async function resetDevice() {
    return api('/api/device/reset', { method: 'POST' })
  }

  async function setPowerOn(voltageMv: 1800 | 3300 | 5000, confirm5v = false) {
    return api('/api/device/power', {
      method: 'POST',
      body: JSON.stringify({
        voltage_mv: voltageMv,
        confirm_5v: confirm5v,
      }),
    })
  }

  async function rebootProbe() {
    const result = await api('/api/device/reboot', { method: 'POST' })
    await refreshStatus()
    return result
  }

  async function eraseDevice() {
    return api('/api/device/erase', { method: 'POST' })
  }

  async function haltDevice() {
    return api('/api/device/halt', { method: 'POST' })
  }

  async function resumeDevice() {
    return api('/api/device/resume', { method: 'POST' })
  }

  async function checkHardfault() {
    return api('/api/device/hardfault')
  }

  function startStatusPolling(intervalMs = 3000) {
    stopStatusPolling()
    refreshStatus()
    statusInterval = setInterval(refreshStatus, intervalMs)
  }

  function stopStatusPolling() {
    if (statusInterval) {
      clearInterval(statusInterval)
      statusInterval = null
    }
  }

  async function setProjectRoot(path: string): Promise<{ project_root: string }> {
    return api('/api/project-root', {
      method: 'PUT',
      body: JSON.stringify({ path }),
    })
  }

  async function getProjectRoot(): Promise<{ project_root: string }> {
    return api('/api/project-root')
  }

  async function browseProjectRoot(path: string): Promise<{
    current: string
    parent: string
    dirs: { name: string; path: string }[]
    drives?: string[]
  }> {
    return api(`/api/project-root/browse?path=${encodeURIComponent(path)}`)
  }

  async function findRtt(sourcePath?: string): Promise<RttFindResponse> {
    return api('/api/rtt-find', {
      method: 'POST',
      body: JSON.stringify(sourcePath ? { source_path: sourcePath } : {}),
    })
  }

  async function writeRtt(data: Uint8Array): Promise<RttWriteResponse> {
    return api('/api/dash/rtt/write', {
      method: 'POST',
      body: JSON.stringify({ data_hex: toHexPayload(data) }),
    })
  }

  async function setRttEncoding(encoding: RttEncoding): Promise<{ encoding: RttEncoding }> {
    return api('/api/dash/rtt/encoding', {
      method: 'POST',
      body: JSON.stringify({ encoding }),
    })
  }

  async function getProjectHistory(): Promise<ProjectHistory> {
    return api('/api/project-history')
  }

  async function addProjectHistory(path: string): Promise<ProjectHistory> {
    return api('/api/project-history', {
      method: 'POST',
      body: JSON.stringify({ path }),
    })
  }

  async function removeProjectHistory(path: string): Promise<ProjectHistory> {
    return api(`/api/project-history?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
    })
  }

  return {
    deviceStatus: readonly(deviceStatus),
    listPorts,
    discoverPort,
    getProfiles,
    getConfig,
    updateConfig,
    uploadFileSource,
    getConfigStatus,
    getProjectInfo,
    getRttConfig,
    updateRttConfig,
    getMicrokeenInfo,
    probeFirmwareCheck,
    connectDevice,
    disconnectDevice,
    refreshStatus,
    flashDevice,
    resetDevice,
    setPowerOn,
    rebootProbe,
    eraseDevice,
    haltDevice,
    resumeDevice,
    checkHardfault,
    parseAxf,
    startStatusPolling,
    stopStatusPolling,
    setProjectRoot,
    getProjectRoot,
    browseProjectRoot,
    findRtt,
    writeRtt,
    setRttEncoding,
    getProjectHistory,
    addProjectHistory,
    removeProjectHistory,
  }
}
