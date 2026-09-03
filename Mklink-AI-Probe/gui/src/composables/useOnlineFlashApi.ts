import type {
  CustomFlmRecord,
  FlashAlgorithmRecord,
  FirmwareSourceStatus,
  ImageInspection,
  JobEvent,
  JobCreateResult,
  JobRequest,
  JobSnapshot,
  JobStreamError,
  JobStreamEvent,
  JobSubscription,
  PackCancelResult,
  PackEvent,
  PackOperationResponse,
  PackRemoveResult,
  PackStatus,
  PreviewPage,
  ProbeRecord,
  TargetRecord,
  TargetMemoryRegion,
  TargetSearchOptions,
  ReadMemoryRequest,
} from '../types/onlineFlash'
import { tr } from './useLanguage'
import { API_BASE } from '../lib/runtimeEndpoint'
const ONLINE_FLASH_BASE = '/api/online-flash'
const TERMINAL_STATES = new Set(['succeeded', 'failed', 'stopped'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue)
  if (!isRecord(value)) return value
  return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, stableValue(value[key])]),
  )
}

function stableJson(value: unknown): string | null {
  try {
    return JSON.stringify(stableValue(value)) ?? null
  } catch {
    return null
  }
}

function errorDetail(payload: unknown): unknown {
  if (isRecord(payload) && Object.prototype.hasOwnProperty.call(payload, 'detail')) {
    return payload.detail
  }
  return payload
}

function stringField(value: unknown, field: string): string | null {
  if (!isRecord(value) || typeof value[field] !== 'string') return null
  return value[field]
}

function errorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map(item => {
      const message = stringField(item, 'msg')
      return message ?? stableJson(item) ?? fallback
    }).join('; ')
  }
  if (isRecord(detail)) {
    const message = stringField(detail, 'message')
    if (message) return message
    const code = stringField(detail, 'code')
    const owner = stringField(detail, 'owner')
    const resource = stringField(detail, 'resource')
    if (code && owner && resource) return `${code}: ${resource} is owned by ${owner}`
    return stableJson(detail) ?? fallback
  }
  if (detail !== null && detail !== undefined) return String(detail)
  return fallback
}

export class OnlineFlashApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly owner: string | null
  readonly resource: string | null
  readonly detail: unknown

  constructor(status: number, fallback: string, payload: unknown) {
    const detail = errorDetail(payload)
    super(errorMessage(detail, fallback || `HTTP ${status}`))
    this.name = 'OnlineFlashApiError'
    this.status = status
    this.code = stringField(detail, 'code')
    this.owner = stringField(detail, 'owner')
    this.resource = stringField(detail, 'resource')
    this.detail = detail
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const isMultipart = options.body instanceof FormData
  const headers = new Headers(options.headers)
  if (!isMultipart && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(`${API_BASE}${ONLINE_FLASH_BASE}${path}`, {
    ...options,
    headers,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new OnlineFlashApiError(response.status, response.statusText, payload)
  }
  return response.json() as Promise<T>
}

async function binaryRequest(path: string, options: RequestInit = {}): Promise<Blob> {
  const headers = new Headers(options.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${ONLINE_FLASH_BASE}${path}`, { ...options, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new OnlineFlashApiError(response.status, response.statusText, payload)
  }
  return response.blob()
}

async function binaryStreamRequest(
  path: string,
  options: RequestInit,
  onProgress: (received: number) => void,
): Promise<Blob> {
  const headers = new Headers(options.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${ONLINE_FLASH_BASE}${path}`, { ...options, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new OnlineFlashApiError(response.status, response.statusText, payload)
  }
  if (!response.body) return response.blob()
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let received = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
    received += value.byteLength
    onProgress(received)
  }
  return new Blob(
    chunks.map(chunk => chunk.slice().buffer as ArrayBuffer),
    { type: response.headers.get('Content-Type') || 'application/octet-stream' },
  )
}

async function packOperationRequest(
  path: string,
  options: RequestInit,
  onEvent?: (event: PackEvent) => void,
): Promise<PackOperationResponse> {
  const isMultipart = options.body instanceof FormData
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/x-ndjson')
  if (!isMultipart && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${ONLINE_FLASH_BASE}${path}`, { ...options, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new OnlineFlashApiError(response.status, response.statusText, payload)
  }

  if (!response.headers.get('Content-Type')?.toLowerCase().includes('application/x-ndjson')) {
    const legacy = await response.json() as PackOperationResponse
    for (const event of legacy.events || []) onEvent?.(event)
    return legacy
  }
  if (!response.body) throw new Error(tr('Pack 操作未返回进度数据流', 'Pack operation did not return a progress stream'))

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const events: PackEvent[] = []
  let result: PackOperationResponse['result'] | null = null
  let buffer = ''

  const consume = (line: string) => {
    if (!line.trim()) return
    const message = JSON.parse(line) as Record<string, unknown>
    if (message.type === 'event' && isRecord(message.event)) {
      const event = message.event as unknown as PackEvent
      if (events.length >= 128) events.shift()
      events.push(event)
      onEvent?.(event)
      return
    }
    if (message.type === 'result' && isRecord(message.result)) {
      result = message.result as unknown as PackOperationResponse['result']
      return
    }
    if (message.type === 'error') {
      const status = typeof message.status === 'number' ? message.status : 500
      throw new OnlineFlashApiError(status, `HTTP ${status}`, { detail: message.detail })
    }
    throw new Error(tr('Pack 操作返回了无效的进度消息', 'Pack operation returned an invalid progress message'))
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
  } catch (error) {
    await reader.cancel().catch(() => undefined)
    throw error
  }
  if (result === null) throw new Error(tr('Pack 操作在返回结果前中断', 'Pack operation ended before returning a result'))
  return { result, events }
}

function encoded(value: string): string {
  return encodeURIComponent(value)
}

export function useOnlineFlashApi() {
  function listProbes(): Promise<ProbeRecord[]> {
    return request('/probes')
  }

  function searchTargets(query = '', options: TargetSearchOptions = {}, signal?: AbortSignal): Promise<TargetRecord[]> {
    const params = new URLSearchParams({ q: query })
    if (options.vendor !== undefined) params.set('vendor', options.vendor)
    if (options.installed !== undefined) params.set('installed', String(options.installed))
    if (options.limit !== undefined) params.set('limit', String(options.limit))
    return request(`/targets?${params.toString()}`, { signal })
  }

  function getTargetMemoryMap(partNumber: string): Promise<TargetMemoryRegion[]> {
    return request(`/targets/${encoded(partNumber)}/memory-map`)
  }

  function listTargetAlgorithms(partNumber: string): Promise<FlashAlgorithmRecord[]> {
    return request(`/targets/${encoded(partNumber)}/algorithms`)
  }

  function getPackStatus(): Promise<PackStatus> {
    return request('/packs/status')
  }

  function updatePackIndex(onEvent?: (event: PackEvent) => void): Promise<PackOperationResponse> {
    return packOperationRequest('/packs/index/update', { method: 'POST' }, onEvent)
  }

  function installPack(partNumber: string, onEvent?: (event: PackEvent) => void): Promise<PackOperationResponse> {
    return packOperationRequest('/packs/install', {
      method: 'POST',
      body: JSON.stringify({ part_number: partNumber }),
    }, onEvent)
  }

  function importPack(file: File, onEvent?: (event: PackEvent) => void): Promise<PackOperationResponse> {
    const body = new FormData()
    body.append('file', file)
    return packOperationRequest('/packs/import', { method: 'POST', body }, onEvent)
  }

  function cancelPackOperation(): Promise<PackCancelResult> {
    return request('/packs/cancel', { method: 'POST' })
  }

  function removePack(packId: string, version: string): Promise<PackRemoveResult> {
    return request(`/packs/${encoded(packId)}/${encoded(version)}`, { method: 'DELETE' })
  }

  function listCustomFlms(partNumber: string): Promise<CustomFlmRecord[]> {
    return request(`/algorithms?part_number=${encoded(partNumber)}`)
  }

  function addCustomFlm(file: File, partNumber: string): Promise<CustomFlmRecord> {
    const body = new FormData()
    body.append('file', file)
    body.append('part_number', partNumber)
    return request('/algorithms', { method: 'POST', body })
  }

  function removeCustomFlm(algorithmId: string, partNumber: string): Promise<{ status: 'removed' }> {
    return request(`/algorithms/${encoded(algorithmId)}?part_number=${encoded(partNumber)}`, {
      method: 'DELETE',
    })
  }

  function inspectImage(
    file: File,
    partNumber: string,
    baseAddress?: number | string | null,
    signal?: AbortSignal,
    capturedFromTarget = false,
  ): Promise<ImageInspection> {
    const body = new FormData()
    body.append('file', file)
    body.append('part_number', partNumber)
    if (baseAddress !== undefined && baseAddress !== null) {
      body.append('base_address', String(baseAddress))
    }
    if (capturedFromTarget) body.append('captured_from_target', 'true')
    return request('/images/inspect', { method: 'POST', body, signal })
  }

  function inspectImagePath(
    path: string,
    partNumber: string,
    baseAddress?: number | string | null,
    signal?: AbortSignal,
  ): Promise<ImageInspection> {
    return request('/images/inspect-path', {
      method: 'POST',
      body: JSON.stringify({
        path,
        part_number: partNumber,
        base_address: baseAddress === undefined || baseAddress === null ? null : String(baseAddress),
      }),
      signal,
    })
  }

  function getImageSourceStatus(path: string, signal?: AbortSignal): Promise<FirmwareSourceStatus> {
    return request(`/images/source-status?path=${encoded(path)}`, { signal })
  }

  function previewImage(
    imageId: string,
    offset = 0,
    length = 4096,
    signal?: AbortSignal,
  ): Promise<PreviewPage> {
    const params = new URLSearchParams({ offset: String(offset), length: String(length) })
    return request(`/images/${encoded(imageId)}/preview?${params.toString()}`, { signal })
  }

  function readMemory(payload: ReadMemoryRequest): Promise<Blob> {
    return binaryRequest('/memory/read', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  }

  function readMemoryStream(payload: ReadMemoryRequest, onProgress: (received: number) => void): Promise<Blob> {
    return binaryStreamRequest('/memory/read-stream', {
      method: 'POST',
      body: JSON.stringify(payload),
    }, onProgress)
  }

  function createJob(job: JobRequest): Promise<JobCreateResult> {
    return request('/jobs', { method: 'POST', body: JSON.stringify(job) })
  }

  function getActiveJob(): Promise<JobSnapshot | null> {
    return request('/jobs/active')
  }

  function getJob(jobId: string): Promise<JobSnapshot> {
    return request(`/jobs/${encoded(jobId)}`)
  }

  function stopJob(jobId: string): Promise<JobSnapshot> {
    return request(`/jobs/${encoded(jobId)}/stop`, { method: 'POST' })
  }

  function subscribeJob(
    jobId: string,
    afterSequence: number,
    onEvent: (event: JobStreamEvent) => void,
    onError?: (error: JobStreamError) => void,
  ): JobSubscription {
    const params = new URLSearchParams({ after: String(afterSequence) })
    const source = new EventSource(
      `${API_BASE}${ONLINE_FLASH_BASE}/jobs/${encoded(jobId)}/events?${params.toString()}`,
    )
    let lastSequence = afterSequence
    let closed = false
    const close = () => {
      if (closed) return
      closed = true
      source.close()
    }
    const reportError = (error: JobStreamError) => {
      if (onError) onError(error)
      else onEvent(error)
    }
    const receive = (event: Event) => {
      if (closed) return
      if (!(event instanceof MessageEvent) || typeof event.data !== 'string') {
        close()
        reportError({ code: 'STREAM_ERROR', message: 'Event stream connection failed' })
        return
      }
      let parsed: JobStreamEvent
      try {
        parsed = JSON.parse(event.data) as JobStreamEvent
      } catch {
        const error: JobStreamError = {
          code: 'STREAM_PARSE_ERROR',
          message: 'Event stream returned invalid JSON',
        }
        close()
        reportError(error)
        return
      }
      if (isRecord(parsed) && typeof parsed.sequence === 'number') {
        const jobEvent = parsed as JobEvent
        if (jobEvent.sequence <= lastSequence) return
        lastSequence = jobEvent.sequence
        if (event.type === 'error' || (jobEvent.state && TERMINAL_STATES.has(jobEvent.state))) {
          try {
            onEvent(jobEvent)
          } finally {
            close()
          }
          return
        }
        onEvent(jobEvent)
        return
      }
      if (event.type === 'error') {
        try {
          onEvent(parsed)
        } finally {
          close()
        }
        return
      }
      onEvent(parsed)
    }
    for (const eventName of ['state', 'progress', 'log', 'error']) {
      source.addEventListener(eventName, receive)
    }
    return { close }
  }

  return {
    listProbes,
    searchTargets,
    listTargetAlgorithms,
    getTargetMemoryMap,
    getPackStatus,
    updatePackIndex,
    installPack,
    importPack,
    cancelPackOperation,
    removePack,
    listCustomFlms,
    addCustomFlm,
    removeCustomFlm,
    inspectImage,
    inspectImagePath,
    getImageSourceStatus,
    previewImage,
    readMemory,
    readMemoryStream,
    createJob,
    getActiveJob,
    getJob,
    stopJob,
    subscribeJob,
  }
}
