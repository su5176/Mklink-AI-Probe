import type {
  OfflineAlgorithmCandidate,
  OfflineConfigPayload,
  OfflineDeployResult,
  OfflineDiskStatus,
  OfflinePreview,
  OfflineTriggerResult,
} from '../types/offlineFlash'
import { tr } from './useLanguage'
import { API_BASE } from '../lib/runtimeEndpoint'

function base(): string {
  return `${API_BASE}/api/offline-download`
}

function resourceOwnerLabel(owner: unknown): string {
  if (typeof owner !== 'string') return tr('其他功能', 'another feature')
  const name = owner.split(':').at(-1)?.toLowerCase()
  if (name === 'superwatch') return 'SuperWatch'
  if (name === 'rtt') return 'RTT View'
  if (name === 'systemview') return 'RTOS Trace'
  if (name === 'vofa') return 'VOFA+'
  return owner
}

function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const value = detail as Record<string, unknown>
    if (value.code === 'PROBE_BUSY') {
      return tr(`探针正被 ${resourceOwnerLabel(value.conflict_owner ?? value.owner)} 占用，请先停止该功能后重试。`, `The probe is in use by ${resourceOwnerLabel(value.conflict_owner ?? value.owner)}. Stop it and retry.`)
    }
    if (typeof value.message === 'string') return value.message
    try { return JSON.stringify(value) } catch { return fallback }
  }
  return fallback
}

async function responseError(response: Response): Promise<Error> {
  const payload = await response.json().catch(() => null)
  return new Error(detailMessage(payload?.detail, response.statusText || `HTTP ${response.status}`))
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${base()}${path}`, {
    ...options,
    headers: options?.body instanceof FormData
      ? options.headers
      : { 'Content-Type': 'application/json', ...options?.headers },
  })
  if (!response.ok) throw await responseError(response)
  return response.json()
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

export function useOfflineFlashApi() {
  function getStatus(): Promise<OfflineDiskStatus> {
    return request('/status')
  }

  function listAlgorithms(partNumber: string): Promise<OfflineAlgorithmCandidate[]> {
    return request(`/algorithms?part_number=${encodeURIComponent(partNumber)}`)
  }

  function preview(config: OfflineConfigPayload): Promise<OfflinePreview> {
    return request('/preview', { method: 'POST', body: JSON.stringify(config) })
  }

  function deploy(
    config: OfflineConfigPayload,
    firmwareFiles: File[],
    flmFiles: File[],
  ): Promise<OfflineDeployResult> {
    const body = new FormData()
    body.append('config_json', JSON.stringify(config))
    firmwareFiles.forEach(file => body.append('firmware_files', file, file.name))
    flmFiles.forEach(file => body.append('flm_files', file, file.name))
    return request('/deploy', { method: 'POST', body })
  }

  async function trigger(
    model: 'V2' | 'V3' | 'V4',
    scriptName: string,
    onLine?: (line: string) => void,
    port?: string,
  ): Promise<OfflineTriggerResult> {
    const headers = new Headers({
      'Content-Type': 'application/json',
      Accept: 'application/x-ndjson',
    })
    const response = await fetch(`${base()}/trigger`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        model,
        script_name: scriptName,
        ...(port ? { port } : {}),
      }),
    })
    if (!response.ok) throw await responseError(response)
    if (!response.headers.get('Content-Type')?.toLowerCase().includes('application/x-ndjson')) {
      return response.json()
    }
    if (!response.body) throw new Error(tr('脱机下载未返回实时日志数据流', 'Offline flashing did not return a live log stream'))

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let result: OfflineTriggerResult | null = null
    const consume = (line: string) => {
      if (!line.trim()) return
      const message = JSON.parse(line) as Record<string, unknown>
      if (message.type === 'line' && typeof message.line === 'string') {
        onLine?.(message.line)
        return
      }
      if (message.type === 'result' && isRecord(message.result)) {
        result = message.result as unknown as OfflineTriggerResult
        return
      }
      if (message.type === 'error') {
        throw new Error(detailMessage(message.detail, tr('脱机下载执行失败', 'Offline flashing failed')))
      }
      throw new Error(tr('脱机下载返回了无效的实时日志消息', 'Offline flashing returned an invalid live log message'))
    }

    try {
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        let newline = buffer.indexOf('\n')
        while (newline >= 0) {
          consume(buffer.slice(0, newline))
          buffer = buffer.slice(newline + 1)
          newline = buffer.indexOf('\n')
        }
        if (done) break
      }
      consume(buffer)
    } catch (value) {
      await reader.cancel().catch(() => undefined)
      throw value
    }
    if (result === null) throw new Error(tr('脱机下载实时日志在返回结果前中断', 'Offline flash log stream ended before returning a result'))
    return result
  }

  return { getStatus, listAlgorithms, preview, deploy, trigger }
}
