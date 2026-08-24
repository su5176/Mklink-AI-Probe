import { readonly, ref, shallowRef } from 'vue'
import type {
  AxfFingerprint,
  SymbolBrowseNode,
  SymbolBrowsePage,
  SymbolCatalogPage,
  SymbolCatalogStatus,
  SymbolCLayoutResult,
  SymbolContainerDescriptor,
  SymbolDescriptor,
  SymbolRebindSummary,
  SymbolSearchResult,
  SuperWatchWriteResult,
} from '../types/mklink'
import { tr } from './useLanguage'
import { API_BASE } from '../lib/runtimeEndpoint'
const PAGE_SIZE = 500

const items = shallowRef<SymbolDescriptor[]>([])
const containers = shallowRef<SymbolContainerDescriptor[]>([])
const generation = ref(0)
const axfPath = ref('')
const parsedAt = ref(0)
const fingerprint = shallowRef<AxfFingerprint | null>(null)
const stale = ref(false)
const total = ref(0)
const truncatedRoots = shallowRef<string[]>([])
const browseRoots = shallowRef<SymbolBrowseNode[]>([])
const browseChildren = shallowRef<Map<string, SymbolBrowseNode[]>>(new Map())
const browseLoading = shallowRef<Set<string>>(new Set())
const loading = ref(false)
const reparsing = ref(false)
const applyingLayout = ref(false)
const error = ref<string | null>(null)

let loadingPromise: Promise<void> | null = null
let forcedRefreshPromise: Promise<void> | null = null

function errorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== 'object') return fallback
  const detail = (payload as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return fallback
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(errorMessage(payload, response.statusText || 'Request failed'))
  }
  return response.json() as Promise<T>
}

async function fetchCatalog(): Promise<SymbolCatalogPage> {
  const first = await request<SymbolCatalogPage>(
    `/api/symbols/catalog?offset=0&limit=${PAGE_SIZE}`,
  )
  const merged = [...first.items]
  while (merged.length < first.total) {
    const page = await request<SymbolCatalogPage>(
      `/api/symbols/catalog?offset=${merged.length}&limit=${PAGE_SIZE}`,
    )
    if (
      page.generation !== first.generation
      || page.axf_path !== first.axf_path
      || page.fingerprint.size !== first.fingerprint.size
      || page.fingerprint.mtime_ns !== first.fingerprint.mtime_ns
    ) {
      throw new Error('Symbol catalog changed while loading; retry')
    }
    if (page.items.length === 0) break
    merged.push(...page.items)
  }
  return { ...first, items: merged, total: merged.length }
}

function publishCatalog(catalog: SymbolCatalogPage): void {
  items.value = catalog.items
  containers.value = catalog.containers ?? []
  generation.value = catalog.generation
  axfPath.value = catalog.axf_path
  parsedAt.value = catalog.parsed_at
  fingerprint.value = catalog.fingerprint
  stale.value = catalog.stale
  total.value = catalog.total
  truncatedRoots.value = catalog.truncated_roots ?? []
  browseRoots.value = catalog.browse_roots ?? []
  browseChildren.value = new Map()
  browseLoading.value = new Set()
}

function sameIdentity(page: SymbolBrowsePage): boolean {
  const current = fingerprint.value
  return page.generation === generation.value
    && page.axf_path === axfPath.value
    && current !== null
    && page.fingerprint.size === current.size
    && page.fingerprint.mtime_ns === current.mtime_ns
}

async function fetchBrowse(path = '', offset: number | null = null): Promise<SymbolBrowsePage> {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE > 256 ? 256 : PAGE_SIZE) })
  if (path) params.set('path', path)
  if (offset !== null) params.set('offset', String(offset))
  return request<SymbolBrowsePage>(`/api/symbols/browse?${params.toString()}`)
}

function setBrowseLoading(key: string, enabled: boolean): void {
  const next = new Set(browseLoading.value)
  if (enabled) next.add(key)
  else next.delete(key)
  browseLoading.value = next
}

async function loadBrowseChildren(node: SymbolBrowseNode): Promise<void> {
  if (node.kind !== 'branch' && node.kind !== 'range') return
  if (browseChildren.value.has(node.key)) return
  if (browseLoading.value.has(node.key)) return
  setBrowseLoading(node.key, true)
  try {
    let page = await fetchBrowse(
      node.path,
      node.kind === 'range' ? node.range_start : null,
    )
    // Stopping/restarting a dashboard can rebind the device catalog while a
    // lazy branch request is in flight. Refresh once and retry the same path
    // so the user does not have to discover and manually click "retry".
    if (!sameIdentity(page)) {
      await ensureLoaded(true)
      page = await fetchBrowse(
        node.path,
        node.kind === 'range' ? node.range_start : null,
      )
    }
    if (!sameIdentity(page)) throw new Error('Symbol catalog changed while loading; retry')
    const next = new Map(browseChildren.value)
    next.set(node.key, page.nodes)
    browseChildren.value = next
  } finally {
    setBrowseLoading(node.key, false)
  }
}

async function searchSymbols(query: string): Promise<SymbolDescriptor[]> {
  const params = new URLSearchParams({ q: query })
  const payload = await request<{ results: SymbolSearchResult[] }>(
    `/api/symbols/search?${params.toString()}`,
  )
  return payload.results.map(result => result.descriptor).filter(Boolean)
}

async function loadCatalog(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    publishCatalog(await fetchCatalog())
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    throw cause
  } finally {
    loading.value = false
  }
}

function startLoad(): Promise<void> {
  if (loadingPromise) return loadingPromise
  loadingPromise = loadCatalog().finally(() => {
    loadingPromise = null
  })
  return loadingPromise
}

async function ensureLoaded(force = false): Promise<void> {
  if (!force && generation.value > 0) {
    await refreshStatus()
    return
  }
  if (!force) return startLoad()
  if (forcedRefreshPromise) return forcedRefreshPromise
  forcedRefreshPromise = (async () => {
    const existingLoad = loadingPromise
    if (existingLoad) await existingLoad.catch(() => undefined)
    await startLoad()
  })().finally(() => {
    forcedRefreshPromise = null
  })
  return forcedRefreshPromise
}

async function refreshStatus(): Promise<SymbolCatalogStatus> {
  const status = await request<SymbolCatalogStatus>('/api/symbols/status')
  const currentFingerprint = fingerprint.value
  const identityChanged = generation.value > 0 && (
    status.generation !== generation.value
    || status.axf_path !== axfPath.value
    || currentFingerprint === null
    || status.fingerprint.size !== currentFingerprint.size
    || status.fingerprint.mtime_ns !== currentFingerprint.mtime_ns
  )
  if (identityChanged) {
    await startLoad()
    return status
  }
  stale.value = status.stale
  if (status.generation === generation.value) {
    parsedAt.value = status.parsed_at
    fingerprint.value = status.fingerprint
    total.value = status.total
    truncatedRoots.value = status.truncated_roots ?? []
  }
  return status
}

async function reparse(): Promise<SymbolRebindSummary> {
  reparsing.value = true
  error.value = null
  try {
    const response = await request<Partial<SymbolRebindSummary>>('/api/symbols/reparse', {
      method: 'POST',
    })
    const nextCatalog = await fetchCatalog()
    publishCatalog(nextCatalog)
    return {
      preserved: response.preserved ?? [],
      updated: response.updated ?? [],
      removed: response.removed ?? [],
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    throw cause
  } finally {
    reparsing.value = false
  }
}

async function applyCLayout(
  variable: string,
  definition: string,
  pack: number | null,
): Promise<SymbolCLayoutResult> {
  applyingLayout.value = true
  error.value = null
  try {
    const response = await request<SymbolCLayoutResult>('/api/symbols/c-layout', {
      method: 'POST',
      body: JSON.stringify({ variable, definition, pack }),
    })
    publishCatalog(await fetchCatalog())
    return response
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    throw cause
  } finally {
    applyingLayout.value = false
  }
}

async function writeSymbol(path: string, value: unknown): Promise<SuperWatchWriteResult> {
  if (stale.value) throw new Error(tr('AXF 已变化，请重新解析符号后再写入', 'AXF changed. Reparse symbols before writing.'))
  if (generation.value <= 0) throw new Error(tr('符号表尚未加载', 'Symbol table is not loaded'))
  return request<SuperWatchWriteResult>('/api/dash/superwatch/write', {
    method: 'POST',
    body: JSON.stringify({ path, generation: generation.value, value }),
  })
}

export function useSymbolCatalog() {
  return {
    items: readonly(items),
    containers: readonly(containers),
    generation: readonly(generation),
    axfPath: readonly(axfPath),
    parsedAt: readonly(parsedAt),
    fingerprint: readonly(fingerprint),
    stale: readonly(stale),
    total: readonly(total),
    truncatedRoots: readonly(truncatedRoots),
    browseRoots: readonly(browseRoots),
    browseChildren: readonly(browseChildren),
    browseLoading: readonly(browseLoading),
    loading: readonly(loading),
    reparsing: readonly(reparsing),
    applyingLayout: readonly(applyingLayout),
    error: readonly(error),
    ensureLoaded,
    refreshStatus,
    reparse,
    applyCLayout,
    writeSymbol,
    loadBrowseChildren,
    searchSymbols,
  }
}
