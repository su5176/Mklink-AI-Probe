import { afterEach, describe, expect, it, vi } from 'vitest'

const firstPage = {
  axf_path: 'C:\\first\\app.axf',
  generation: 1,
  parsed_at: 10,
  fingerprint: { size: 100, mtime_ns: 200 },
  stale: false,
  truncated_roots: ['controller'],
  containers: [],
  browse_roots: [
    {
      key: 'controller', path: 'controller', label: 'controller', kind: 'branch',
      type_name: 'Controller', size: 8, address: 0x20000024,
      descriptor: null, container: null, child_count: 1,
      range_start: null, range_end: null,
    },
    {
      key: 'gain', path: 'gain', label: 'gain', kind: 'leaf',
      type_name: 'float', size: 4, address: 0x20000020,
      descriptor: null, container: null, child_count: null,
      range_start: null, range_end: null,
    },
  ],
  total: 2,
  items: [
    {
      path: 'controller.target',
      address: 0x20000024,
      type_name: 'float',
      scalar_kind: 'float',
      size: 4,
      writable: true,
      enum_values: {},
      parent_path: 'controller',
    },
    {
      path: 'gain',
      address: 0x20000020,
      type_name: 'float',
      scalar_kind: 'float',
      size: 4,
      writable: true,
      enum_values: {},
      parent_path: null,
    },
  ],
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
    headers: { 'Content-Type': 'application/json' },
  })
}

async function freshCatalog() {
  vi.resetModules()
  return (await import('./useSymbolCatalog')).useSymbolCatalog()
}

describe('useSymbolCatalog', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('loads the catalog once and shares it across consumers', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(firstPage))
    vi.stubGlobal('fetch', fetchMock)
    vi.resetModules()
    const { useSymbolCatalog } = await import('./useSymbolCatalog')
    const first = useSymbolCatalog()
    const second = useSymbolCatalog()

    await Promise.all([first.ensureLoaded(), second.ensureLoaded()])

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(second.items.value.map(item => item.path)).toEqual(['controller.target', 'gain'])
    expect(second.generation.value).toBe(1)
    expect(second.truncatedRoots.value).toEqual(['controller'])
    expect(second.browseRoots.value.map(node => node.path)).toEqual(['controller', 'gain'])
  })

  it('loads and caches one lazy child page with catalog identity checks', async () => {
    const root = firstPage.browse_roots[0]
    const childPage = {
      generation: firstPage.generation,
      axf_path: firstPage.axf_path,
      fingerprint: firstPage.fingerprint,
      parent: 'controller',
      nodes: [{
        key: 'controller.target', path: 'controller.target', label: 'target', kind: 'leaf',
        type_name: 'float', size: 4, address: 0x20000024,
        descriptor: firstPage.items[0], container: null, child_count: null,
        range_start: null, range_end: null,
      }],
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse(childPage))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    await symbols.loadBrowseChildren(root as any)
    await symbols.loadBrowseChildren(root as any)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1][0]).toContain('/api/symbols/browse?')
    expect(fetchMock.mock.calls[1][0]).toContain('path=controller')
    expect(symbols.browseChildren.value.get('controller')?.[0].path).toBe('controller.target')
  })

  it('refreshes and retries a lazy branch after the catalog is rebound', async () => {
    const reboundPage = {
      ...firstPage,
      generation: 2,
      items: firstPage.items,
      browse_roots: firstPage.browse_roots,
    }
    const reboundChildPage = {
      ...firstPage,
      generation: 2,
      parent: 'controller',
      nodes: [{
        key: 'controller.target', path: 'controller.target', label: 'target', kind: 'leaf',
        type_name: 'float', size: 4, address: 0x20000024,
        descriptor: firstPage.items[0], container: null, child_count: null,
        range_start: null, range_end: null,
      }],
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse({
        ...reboundChildPage,
        generation: 2,
      }))
      .mockResolvedValueOnce(jsonResponse(reboundPage))
      .mockResolvedValueOnce(jsonResponse(reboundChildPage))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    await symbols.loadBrowseChildren(firstPage.browse_roots[0] as any)

    expect(fetchMock).toHaveBeenCalledTimes(4)
    expect(symbols.generation.value).toBe(2)
    expect(symbols.browseChildren.value.get('controller')?.[0].path).toBe('controller.target')
  })

  it('queues a forced refresh behind an in-flight catalog load', async () => {
    let resolveInitial!: (response: Response) => void
    const initial = new Promise<Response>(resolve => { resolveInitial = resolve })
    const nextPage = {
      ...firstPage,
      generation: 2,
      total: 1,
      items: [{ ...firstPage.items[1], path: 'rgb_framebuffer' }],
    }
    const fetchMock = vi.fn()
      .mockReturnValueOnce(initial)
      .mockResolvedValueOnce(jsonResponse(nextPage))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    const firstLoad = symbols.ensureLoaded()
    const forcedRefresh = symbols.ensureLoaded(true)
    resolveInitial(jsonResponse(firstPage))
    await Promise.all([firstLoad, forcedRefresh])

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(symbols.generation.value).toBe(2)
    expect(symbols.items.value.map(item => item.path)).toEqual(['rgb_framebuffer'])
  })

  it('retries a forced refresh after an older in-flight load fails', async () => {
    let resolveInitial!: (response: Response) => void
    const initial = new Promise<Response>(resolve => { resolveInitial = resolve })
    const nextPage = {
      ...firstPage,
      generation: 2,
      total: 1,
      items: [{ ...firstPage.items[1], path: 'rgb_framebuffer' }],
    }
    const fetchMock = vi.fn()
      .mockReturnValueOnce(initial)
      .mockResolvedValueOnce(jsonResponse(nextPage))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    const firstLoad = symbols.ensureLoaded()
    const forcedRefresh = symbols.ensureLoaded(true)
    resolveInitial(jsonResponse({ detail: 'old device disconnected' }, 409))

    await expect(firstLoad).rejects.toThrow('old device disconnected')
    await expect(forcedRefresh).resolves.toBeUndefined()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(symbols.generation.value).toBe(2)
  })

  it('reloads when a reconnected device reuses the same generation', async () => {
    const nextPage = {
      ...firstPage,
      axf_path: 'C:\\second\\app.axf',
      total: 1,
      items: [{ ...firstPage.items[1], path: 'second_device_value' }],
      truncated_roots: [],
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse({
        loaded: true,
        generation: 1,
        axf_path: nextPage.axf_path,
        parsed_at: 20,
        fingerprint: firstPage.fingerprint,
        stale: false,
        total: 1,
        truncated_roots: [],
      }))
      .mockResolvedValueOnce(jsonResponse(nextPage))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    await symbols.ensureLoaded()

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(symbols.items.value.map(item => item.path)).toEqual(['second_device_value'])
    expect(symbols.axfPath.value).toBe(nextPage.axf_path)
  })

  it('refreshes stale status without replacing the loaded catalog', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse({
        loaded: true,
        generation: 1,
        axf_path: firstPage.axf_path,
        parsed_at: 10,
        fingerprint: firstPage.fingerprint,
        stale: true,
        total: 2,
      }))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    await symbols.refreshStatus()

    expect(symbols.stale.value).toBe(true)
    expect(symbols.items.value.map(item => item.path)).toEqual(['controller.target', 'gain'])
  })

  it('reparses and atomically replaces the catalog', async () => {
    const nextPage = {
      ...firstPage,
      generation: 2,
      stale: false,
      items: [{ ...firstPage.items[1], address: 0x20000040 }],
      total: 1,
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse({ preserved: [], updated: ['gain'], removed: ['controller.target'] }))
      .mockResolvedValueOnce(jsonResponse(nextPage))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    const summary = await symbols.reparse()

    expect(summary).toEqual({ preserved: [], updated: ['gain'], removed: ['controller.target'] })
    expect(symbols.generation.value).toBe(2)
    expect(symbols.items.value).toHaveLength(1)
    expect(symbols.items.value[0].address).toBe(0x20000040)
  })

  it('applies a C layout and publishes the expanded catalog', async () => {
    const unresolvedPage = {
      ...firstPage,
      total: 0,
      items: [],
      containers: [{
        path: 'data_save', address: 0x20000648, type_name: 'DATASAVE_TYPEDEF',
        size: 16, reason: 'unsupported_layout',
      }],
    }
    const expandedPage = {
      ...firstPage,
      generation: 2,
      total: 1,
      items: [{ ...firstPage.items[1], path: 'data_save.odo', address: 0x20000648 }],
      containers: [],
    }
    const result = {
      layout: { type_name: 'DATASAVE_TYPEDEF', size: 16, alignment: 8, pack: null, leaf_count: 1 },
      rebind: { preserved: [], updated: [], removed: [] },
      generation: 2,
      axf_path: firstPage.axf_path,
      total: 1,
      container_count: 0,
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(unresolvedPage))
      .mockResolvedValueOnce(jsonResponse(result))
      .mockResolvedValueOnce(jsonResponse(expandedPage))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    await expect(symbols.applyCLayout(
      'data_save',
      'typedef struct { uint64_t odo; } DATASAVE_TYPEDEF;',
      null,
    )).resolves.toEqual(result)

    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/symbols/c-layout', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        variable: 'data_save',
        definition: 'typedef struct { uint64_t odo; } DATASAVE_TYPEDEF;',
        pack: null,
      }),
    }))
    expect(symbols.items.value[0].path).toBe('data_save.odo')
    expect(symbols.containers.value).toEqual([])
  })

  it('keeps the previous catalog when reparse fails', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse({ detail: { phase: 'reparse', message: 'bad DWARF' } }, 409))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    await expect(symbols.reparse()).rejects.toThrow('bad DWARF')

    expect(symbols.generation.value).toBe(1)
    expect(symbols.items.value).toHaveLength(2)
  })

  it('writes a typed value with the selected catalog generation', async () => {
    const result = { path: 'gain', generation: 1, value: 1.3, verified: true }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse(result))
    vi.stubGlobal('fetch', fetchMock)
    const symbols = await freshCatalog()

    await symbols.ensureLoaded()
    await expect(symbols.writeSymbol('gain', 1.3)).resolves.toEqual(result)

    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/dash/superwatch/write',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ path: 'gain', generation: 1, value: 1.3 }),
      }),
    )
  })
})
