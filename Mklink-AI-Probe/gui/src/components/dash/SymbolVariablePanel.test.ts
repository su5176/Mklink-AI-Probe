import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref, shallowRef } from 'vue'

const mocks = vi.hoisted(() => ({
  ensureLoaded: vi.fn(),
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
    generation: ref(1),
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

describe('SymbolVariablePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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

  it('adds and removes a selected variable through the SuperWatch API', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(okJson({ items: [{ name: 'gain' }] }))
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
    expect(fetchMock).toHaveBeenCalledTimes(1)
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

  it('selects and clears a one-dimensional array snapshot curve', async () => {
    mocks.browseRoots.value = [{
      key: 'values', path: 'values', label: 'values', kind: 'branch',
      type_name: 'float[]', size: 128 * 4, address: 0x20001000,
      descriptor: null, container: null, child_count: 128, range_start: null, range_end: null,
      array_dimensions: [128], snapshot_eligible: true,
    }]
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(okJson({ items: [] }))
      .mockResolvedValueOnce(okJson({ snapshot: { name: 'values', count: 128 } }))
      .mockResolvedValueOnce(okJson({ snapshot: null }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SymbolVariablePanel, {
      props: { deviceConnected: true, latestValues: {}, snapshotPath: null },
    })
    await flushPromises()

    await wrapper.get('[data-testid="snapshot-values"]').trigger('click')
    await flushPromises()
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/dash/superwatch/array-snapshot/select',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'values' }) }),
    )
    expect(wrapper.emitted('snapshot-change')).toEqual([['values']])

    await wrapper.setProps({ snapshotPath: 'values' })
    await wrapper.get('[data-testid="snapshot-values"]').trigger('click')
    await flushPromises()
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/dash/superwatch/array-snapshot/clear',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(wrapper.emitted('snapshot-change')).toEqual([['values'], [null]])
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
    await secondRange!.get('button').trigger('click')
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
})
