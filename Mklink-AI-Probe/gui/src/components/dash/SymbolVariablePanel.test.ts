import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref, shallowRef } from 'vue'

const mocks = vi.hoisted(() => ({
  ensureLoaded: vi.fn(),
  refreshStatus: vi.fn(),
  generation: null as any,
  reparse: vi.fn(),
  applyCLayout: vi.fn(),
  writeSymbol: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
  stale: { value: false },
  items: null as any,
  containers: null as any,
  browseRoots: null as any,
  browseChildren: null as any,
  browseLoading: null as any,
  loadBrowseChildren: vi.fn(),
  searchSymbols: vi.fn(),
  applyingLayout: { value: false },
  error: { value: null as string | null },
}))

const catalogItems = [
  {
    path: 'controller.enabled', address: 0x20000028, type_name: 'bool',
    scalar_kind: 'bool', size: 1, writable: true, enum_values: {}, parent_path: 'controller',
  },
  {
    path: 'controller.target', address: 0x20000024, type_name: 'float',
    scalar_kind: 'float', size: 4, writable: true, enum_values: {}, parent_path: 'controller',
  },
  {
    path: 'gain', address: 0x20000020, type_name: 'float',
    scalar_kind: 'float', size: 4, writable: true, enum_values: {}, parent_path: null,
  },
]

const catalogContainers = [{
  path: 'data_save', address: 0x20000648, type_name: 'DATASAVE_TYPEDEF',
  size: 32, reason: 'unsupported_layout',
}]

function browseLeaf(descriptor: typeof catalogItems[number]) {
  return {
    key: descriptor.path,
    path: descriptor.path,
    label: descriptor.path.split('.').at(-1)!,
    kind: 'leaf',
    type_name: descriptor.type_name,
    size: descriptor.size,
    address: descriptor.address,
    descriptor,
    container: null,
    child_count: null,
    range_start: null,
    range_end: null,
  }
}

const defaultBrowseRoots = [
  {
    key: 'controller', path: 'controller', label: 'controller', kind: 'branch',
    type_name: 'Controller', size: 8, address: 0x20000024,
    descriptor: null, container: null, child_count: 2, range_start: null, range_end: null,
  },
  browseLeaf(catalogItems[2]),
  {
    key: 'data_save', path: 'data_save', label: 'data_save', kind: 'container',
    type_name: 'DATASAVE_TYPEDEF', size: 32, address: 0x20000648,
    descriptor: null, container: catalogContainers[0], child_count: null,
    range_start: null, range_end: null,
  },
]

vi.mock('../../composables/useSymbolCatalog', () => ({
  useSymbolCatalog: () => ({
    items: mocks.items ??= shallowRef(catalogItems),
    containers: mocks.containers ??= shallowRef(catalogContainers),
    generation: mocks.generation ??= ref(1),
    stale: mocks.stale,
    truncatedRoots: shallowRef(['controller']),
    browseRoots: mocks.browseRoots ??= shallowRef(defaultBrowseRoots),
    browseChildren: mocks.browseChildren ??= shallowRef(new Map()),
    browseLoading: mocks.browseLoading ??= shallowRef(new Set()),
    loading: ref(false),
    reparsing: ref(false),
    applyingLayout: mocks.applyingLayout,
    error: mocks.error,
    ensureLoaded: mocks.ensureLoaded,
    refreshStatus: mocks.refreshStatus,
    reparse: mocks.reparse,
    applyCLayout: mocks.applyCLayout,
    writeSymbol: mocks.writeSymbol,
    loadBrowseChildren: mocks.loadBrowseChildren,
    searchSymbols: mocks.searchSymbols,
  }),
}))

vi.mock('../../composables/useToast', () => ({
  useToast: () => ({ error: mocks.toastError, success: mocks.toastSuccess }),
}))

import SymbolVariablePanel from './SymbolVariablePanel.vue'

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockPinnedWorkspace(initial: string[] = []) {
  let pins = [...initial]
  let revision = 1
  let available = catalogItems
  let watches = ['gain']
  const payload = () => ({
    pins, revision: String(revision), generation: mocks.generation.value,
    entries: pins.map(path => ({ path, descriptor: available.find(item => item.path === path) ?? null })),
  })
  const fetch = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/pins')) {
      if (options?.method === 'PUT') {
        pins = JSON.parse(String(options.body)).pins
        revision += 1
      }
      return okJson(payload())
    }
    if (url.endsWith('/remove')) watches = watches.filter(path => path !== JSON.parse(String(options?.body)).name)
    if (url.endsWith('/add')) watches.push(JSON.parse(String(options?.body)).name)
    return okJson({ items: watches.map(name => ({ name })) })
  })
  vi.stubGlobal('fetch', fetch)
  return { fetch, pins: () => pins, setAvailable: (items: typeof catalogItems) => { available = items } }
}

describe('SymbolVariablePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.generation ??= ref(1)
    mocks.generation.value = 1
    mocks.stale.value = false
    mocks.items ??= shallowRef(catalogItems)
    mocks.items.value = catalogItems
    mocks.containers ??= shallowRef(catalogContainers)
    mocks.containers.value = catalogContainers
    mocks.browseRoots ??= shallowRef(defaultBrowseRoots)
    mocks.browseRoots.value = defaultBrowseRoots
    mocks.browseChildren ??= shallowRef(new Map())
    mocks.browseChildren.value = new Map()
    mocks.browseLoading ??= shallowRef(new Set())
    mocks.browseLoading.value = new Set()
    mocks.applyingLayout.value = false
    mocks.ensureLoaded.mockResolvedValue(undefined)
    mocks.reparse.mockResolvedValue({ preserved: ['gain'], updated: [], removed: [] })
    mocks.writeSymbol.mockResolvedValue({ path: 'gain', generation: 1, value: 1.3, verified: true })
    mocks.applyCLayout.mockResolvedValue({
      layout: { leaf_count: 3 },
      rebind: { preserved: [], updated: [], removed: [] },
    })
    mocks.loadBrowseChildren.mockImplementation(async (node: { key: string }) => {
      const next = new Map(mocks.browseChildren.value)
      if (node.key === 'controller') {
        next.set('controller', catalogItems.slice(0, 2).map(browseLeaf))
      }
      mocks.browseChildren.value = next
    })
    mocks.searchSymbols.mockImplementation(async (query: string) => catalogItems.filter(item => (
      item.path.includes(query) || item.type_name.includes(query)
    )))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okJson({ items: [{ name: 'gain' }] })))
  })

  it('shows scalars immediately and keeps structured variables collapsed by default', async () => {
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: { gain: 1.25 } },
    })
    await flushPromises()

    expect(mocks.ensureLoaded).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('controller')
    expect(wrapper.find('[data-testid="leaf-controller.target"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="branch-controller"]').text()).toContain('0 / 2')
    expect(wrapper.get('[data-testid="latest-gain"]').text()).toContain('1.25')
    expect(wrapper.text()).not.toContain('前 256 个')

    await wrapper.get('[data-testid="branch-controller"]').trigger('click')
    expect(wrapper.get('[data-testid="leaf-controller.target"]').exists()).toBe(true)
  })

  it('pins selected variables separately from sampling and keeps pins while searching', async () => {
    const server = mockPinnedWorkspace()
    const wrapper = mount(SymbolVariablePanel, { props: { deviceConnected: true, latestValues: { gain: 1.25 } } })
    await flushPromises()
    await wrapper.get('[data-testid="pin-selected"]').trigger('click')
    await flushPromises()
    expect(server.pins()).toEqual(['gain'])
    expect(wrapper.get('[data-testid="pinned-variables"] [data-testid="latest-gain"]').text()).toContain('1.25')
    expect(wrapper.findAll('[data-testid="leaf-gain"]')).toHaveLength(1)
    expect(server.fetch.mock.calls.some(([url]) => url.endsWith('/add'))).toBe(false)
    await wrapper.get('[data-testid="toggle-gain"]').setValue(false)
    await flushPromises()
    expect(server.pins()).toEqual(['gain'])
    await wrapper.get('[data-testid="variable-search"]').setValue('target')
    await flushPromises()
    expect(wrapper.get('[data-testid="pinned-variables"]').text()).toContain('gain')
    expect(wrapper.get('[data-testid="all-variables"]').text()).toContain('target')
    wrapper.unmount()
    const reopened = mount(SymbolVariablePanel, { props: { deviceConnected: true, latestValues: {} } })
    await flushPromises()
    expect(reopened.get('[data-testid="pinned-variables"]').text()).toContain('gain')
    expect((reopened.get('[data-testid="toggle-gain"]').element as HTMLInputElement).checked).toBe(false)
    reopened.unmount()
  })

  it('reorders/unpins without removing a watch and keeps missing favorites disabled', async () => {
    const server = mockPinnedWorkspace(['gain', 'controller.target', 'removed'])
    const wrapper = mount(SymbolVariablePanel, { props: { deviceConnected: true, latestValues: {} } })
    await flushPromises()
    expect(wrapper.get('[data-testid="missing-pin-removed"] input').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="上移 controller.target"]').trigger('click')
    await flushPromises()
    expect(server.pins()).toEqual(['controller.target', 'gain', 'removed'])
    await wrapper.get('[data-testid="pin-gain"]').trigger('click')
    await flushPromises()
    expect(server.pins()).toEqual(['controller.target', 'removed'])
    expect((wrapper.get('[data-testid="toggle-gain"]').element as HTMLInputElement).checked).toBe(true)
    expect(server.fetch.mock.calls.some(([url]) => url.endsWith('/remove'))).toBe(false)
    wrapper.unmount()
  })

  it('re-resolves favorites when the symbol generation changes without auto-selecting', async () => {
    const server = mockPinnedWorkspace(['gain'])
    const wrapper = mount(SymbolVariablePanel, { props: { deviceConnected: true, latestValues: {} } })
    await flushPromises()
    server.setAvailable([])
    mocks.generation.value = 2
    await flushPromises()
    expect(wrapper.find('[data-testid="missing-pin-gain"]').exists()).toBe(true)
    server.setAvailable(catalogItems.map(item => ({ ...item, address: item.address + 64 })))
    mocks.generation.value = 3
    await flushPromises()
    expect(wrapper.find('[data-testid="missing-pin-gain"]').exists()).toBe(false)
    expect(server.fetch.mock.calls.some(([url]) => url.endsWith('/add'))).toBe(false)
    wrapper.unmount()
  })

  it('shows all multi-keyword matches in the unpinned directory', async () => {
    mockPinnedWorkspace()
    mocks.searchSymbols.mockResolvedValue(catalogItems)
    const wrapper = mount(SymbolVariablePanel, { props: { deviceConnected: true, latestValues: {} } })
    await flushPromises()
    await wrapper.get('[data-testid="variable-search"]').setValue('gain, target')
    await flushPromises()
    const list = wrapper.get('[data-testid="all-variables"]')
    expect(list.find('[data-testid="leaf-gain"]').exists()).toBe(true)
    expect(list.find('[data-testid="leaf-controller.target"]').exists()).toBe(true)
    expect(list.find('[data-testid="leaf-controller.enabled"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('refreshes an automatically replaced symbol generation and stops polling on unmount', async () => {
    vi.useFakeTimers()
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    try {
      await flushPromises()
      mocks.refreshStatus.mockImplementationOnce(async () => { mocks.generation.value = 2 })
      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(mocks.refreshStatus).toHaveBeenCalledOnce()
      expect(mocks.ensureLoaded).toHaveBeenCalledTimes(2)
      expect(wrapper.text()).toContain('符号已重载，采集已停止')
      wrapper.unmount()
      await vi.advanceTimersByTimeAsync(4000)
      expect(mocks.refreshStatus).toHaveBeenCalledOnce()
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('adds and removes a selected variable through the SuperWatch API', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(okJson({ items: [{ name: 'gain' }] }))
      .mockResolvedValueOnce(okJson({ pins: [], revision: '1', entries: [] }))
      .mockResolvedValueOnce(okJson({ item: { name: 'controller.target' } }))
      .mockResolvedValueOnce(okJson({ item: { name: 'gain', removed: true } }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {}, hiddenChannels: new Set<string>() },
    })
    await flushPromises()

    await wrapper.get('[data-testid="branch-controller"]').trigger('click')
    await wrapper.get('[data-testid="toggle-controller.target"]').setValue(true)
    await flushPromises()
    await wrapper.get('[data-testid="toggle-gain"]').setValue(false)
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/dash/superwatch/add', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ name: 'controller.target' }),
    }))
    expect(fetchMock).toHaveBeenCalledWith('/api/dash/superwatch/remove', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ name: 'gain' }),
    }))
    expect(wrapper.emitted('selection-removed')).toEqual([['gain']])
  })

  it('adds a manually entered member path through the shared SuperWatch API', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(okJson({ items: [] }))
      .mockResolvedValueOnce(okJson({ pins: [], revision: '1', entries: [] }))
      .mockResolvedValueOnce(okJson({ item: { name: 'data_save.odo', type: 'uint64_t' } }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    await flushPromises()

    await wrapper.get('[data-testid="show-manual-add"]').trigger('click')
    await wrapper.get('[data-testid="manual-variable-path"]').setValue('data_save.odo')
    await wrapper.get('[data-testid="add-manual-variable"]').trigger('submit')
    await flushPromises()

    expect(fetchMock).toHaveBeenLastCalledWith('/api/dash/superwatch/add', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ name: 'data_save.odo' }),
    }))
    expect(wrapper.text()).toContain('已选 1')
  })

  it('opens an unresolved container and applies its pasted C definition', async () => {
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="container-data_save"]').text()).toContain('待定义')
    await wrapper.get('[data-testid="container-data_save"]').trigger('click')
    expect(wrapper.get('[data-testid="c-layout-variable"]').element).toHaveProperty('value', 'data_save')
    await wrapper.get('[data-testid="c-layout-definition"]').setValue(
      'typedef struct { uint64_t odo; } DATASAVE_TYPEDEF;',
    )
    await wrapper.get('[data-testid="c-layout-pack"]').setValue('4')
    await wrapper.get('[data-testid="apply-c-layout"]').trigger('click')
    await flushPromises()

    expect(mocks.applyCLayout).toHaveBeenCalledWith(
      'data_save',
      'typedef struct { uint64_t odo; } DATASAVE_TYPEDEF;',
      4,
    )
    expect(mocks.toastSuccess).toHaveBeenCalledWith('已解析 3 个成员')
    expect(wrapper.find('[data-testid="c-layout-modal"]').exists()).toBe(false)
  })

  it('shows an eye only for selected variables and toggles rendering without changing acquisition', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ items: [{ name: 'gain' }] }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {}, hiddenChannels: new Set<string>() },
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="visibility-controller.target"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="visibility-gain"]').attributes('aria-pressed')).toBe('true')

    await wrapper.get('[data-testid="visibility-gain"]').trigger('click')

    expect(wrapper.emitted('visibility-change')).toEqual([['gain', false]])
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
  })

  it('shows the hidden state without removing the selected variable', async () => {
    const wrapper = mount(SymbolVariablePanel, {
      props: {
        deviceConnected: true,
        latestValues: { gain: 1.25 },
        hiddenChannels: new Set(['gain']),
      },
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="toggle-gain"]').attributes('checked')).toBeDefined()
    expect(wrapper.get('[data-testid="visibility-gain"]').attributes('aria-pressed')).toBe('false')
    expect(wrapper.get('[data-testid="latest-gain"]').text()).toContain('1.25')
  })

  it('writes a float from the variable row and shows the verified value', async () => {
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: { gain: 1.25 } },
    })
    await flushPromises()
    await wrapper.get('[data-testid="edit-gain"]').trigger('click')
    await wrapper.get('[data-testid="write-input-gain"]').setValue('1.3')
    await wrapper.get('[data-testid="write-gain"]').trigger('click')
    await flushPromises()

    expect(mocks.writeSymbol).toHaveBeenCalledWith('gain', 1.3)
    expect(wrapper.get('[data-testid="write-ok-gain"]').text()).toContain('1.3')
  })

  it('refuses writes while the AXF catalog is stale', async () => {
    mocks.stale.value = true
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: { gain: 1.25 } },
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="edit-gain"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('AXF 已变化')
  })

  it('expands search matches and restores the previous expansion state when cleared', async () => {
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    await flushPromises()

    await wrapper.get('[data-testid="variable-search"]').setValue('target')
    expect(wrapper.get('[data-testid="leaf-controller.target"]').exists()).toBe(true)

    await wrapper.get('[data-testid="variable-search"]').setValue('')
    expect(wrapper.find('[data-testid="leaf-controller.target"]').exists()).toBe(false)
  })

  it('shows only selected leaves and their ancestors in selected-only mode', async () => {
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: { gain: 1.25 } },
    })
    await flushPromises()

    await wrapper.get('[data-testid="selected-only"]').setValue(true)
    expect(wrapper.get('[data-testid="leaf-gain"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="branch-controller"]').exists()).toBe(false)
  })

  it('prunes the saved expansion snapshot when reparse removes branches during search', async () => {
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    await flushPromises()

    await wrapper.get('[data-testid="branch-controller"]').trigger('click')
    await wrapper.get('[data-testid="variable-search"]').setValue('target')
    mocks.items.value = [catalogItems[2]]
    mocks.reparse.mockImplementationOnce(async () => {
      mocks.browseChildren.value = new Map()
      return {
        preserved: ['gain'], updated: [], removed: ['controller.enabled', 'controller.target'],
      }
    })
    await wrapper.get('[data-testid="reparse-symbols"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="variable-search"]').setValue('')
    mocks.items.value = catalogItems
    await nextTick()

    expect(wrapper.get('[data-testid="branch-controller"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="leaf-controller.target"]').exists()).toBe(false)
  })

  it('mounts only the lazy root for an array with thousands of elements', async () => {
    mocks.browseRoots.value = [{
      key: 'values', path: 'values', label: 'values', kind: 'branch',
      type_name: 'float[]', size: 4660 * 4, address: 0x20001000,
      descriptor: null, container: null, child_count: 4660, range_start: null, range_end: null,
    }]
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    await flushPromises()

    expect(wrapper.findAll('.variable-row')).toHaveLength(0)
    expect(wrapper.findAll('.branch-row')).toHaveLength(1)
  })

  it('opens a chosen 256-element range without loading earlier ranges', async () => {
    const valueDescriptor = (index: number) => ({
      ...catalogItems[2],
      path: `values[${index}]`,
      address: 0x20001000 + index * 4,
      type_name: 'uint32_t',
      scalar_kind: 'unsigned',
      parent_path: 'values',
    })
    mocks.browseRoots.value = [{
      key: 'values', path: 'values', label: 'values', kind: 'branch',
      type_name: 'uint32_t[]', size: 1000 * 4, address: 0x20001000,
      descriptor: null, container: null, child_count: 1000, range_start: null, range_end: null,
    }]
    mocks.loadBrowseChildren.mockImplementation(async (node: { key: string }) => {
      const next = new Map(mocks.browseChildren.value)
      if (node.key === 'values') {
        next.set('values', [0, 256, 512, 768].map(start => ({
          key: `values::range:${start}:${Math.min(start + 255, 999)}`,
          path: 'values', label: `[${start}..${Math.min(start + 255, 999)}]`, kind: 'range',
          type_name: 'uint32_t[]', size: 0, address: 0x20001000,
          descriptor: null, container: null,
          child_count: Math.min(256, 1000 - start), range_start: start, range_end: Math.min(start + 255, 999),
        })))
      } else if (node.key === 'values::range:256:511') {
        next.set(node.key, [browseLeaf(valueDescriptor(256)), browseLeaf(valueDescriptor(511))])
      }
      mocks.browseChildren.value = next
    })
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    await flushPromises()

    await wrapper.get('[data-testid="branch-values"]').trigger('click')
    await flushPromises()
    const secondRange = wrapper.findAll('.branch-row').find(row => row.text().includes('[256..511]'))
    expect(secondRange).toBeDefined()
    await secondRange!.trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="leaf-values[256]"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="leaf-values[511]"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="leaf-values[0]"]').exists()).toBe(false)
  })

  it('shows an exact unloaded array path returned by backend search', async () => {
    const descriptor = {
      ...catalogItems[2],
      path: 'values[999]',
      address: 0x20001000 + 999 * 4,
      type_name: 'uint32_t',
      scalar_kind: 'unsigned',
      parent_path: 'values',
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okJson({ items: [] })))
    mocks.searchSymbols.mockImplementation(async (query: string) => (
      query === 'values[999]' ? [descriptor] : []
    ))
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {} },
    })
    await flushPromises()

    await wrapper.get('[data-testid="variable-search"]').setValue('values[999]')
    await flushPromises()

    expect(wrapper.get('[data-testid="leaf-values[999]"]').exists()).toBe(true)
  })

  it('allows collapsing and reopening an array while searching, then restores browsing state', async () => {
    mocks.searchSymbols.mockResolvedValue([{ ...catalogItems[2], path: 'samples[0]', parent_path: 'samples' }])
    const wrapper = mount(SymbolVariablePanel, { props: { deviceConnected: true, latestValues: {} } })
    await flushPromises()
    await wrapper.get('[data-testid="variable-search"]').setValue('samples')
    await flushPromises()
    const branch = wrapper.get('[data-testid="branch-samples"]')
    expect(branch.attributes('aria-expanded')).toBe('true')
    await branch.trigger('click')
    expect(wrapper.find('[data-testid="leaf-samples[0]"]').exists()).toBe(false)
    expect(branch.attributes('aria-expanded')).toBe('false')
    await branch.trigger('click')
    expect(wrapper.find('[data-testid="leaf-samples[0]"]').exists()).toBe(true)
    await wrapper.get('[data-testid="variable-search"]').setValue('')
    await flushPromises()
    expect(wrapper.find('[data-testid="leaf-controller.target"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('configures a bounded array snapshot without changing element selection', async () => {
    mocks.browseRoots.value = [{
      key: 'samples', path: 'samples', label: 'samples', kind: 'branch',
      type_name: 'int16_t[]', size: 512 * 2, address: 0x20001000,
      descriptor: null, container: null, child_count: 512, range_start: null, range_end: null,
      array_dimensions: [512], snapshot_eligible: true,
    }]
    const fetchMock = vi.fn().mockResolvedValue(okJson({
      snapshot: { name: 'samples', start_index: 64, count: 32 },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {}, snapshotPath: null },
    })
    await flushPromises()

    await wrapper.get('[data-testid="snapshot-samples"]').trigger('click')
    expect(wrapper.get('[data-testid="array-snapshot-modal"]').exists()).toBe(true)
    await wrapper.get('[data-testid="array-snapshot-start"]').setValue('64')
    await wrapper.get('[data-testid="array-snapshot-count"]').setValue('32')
    await wrapper.get('[data-testid="array-snapshot-confirm"]').trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/dash/superwatch/array-snapshot/select',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'samples', start_index: 64, count: 32 }),
      }),
    )
    expect(wrapper.emitted('snapshot-change')).toEqual([['samples']])
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
