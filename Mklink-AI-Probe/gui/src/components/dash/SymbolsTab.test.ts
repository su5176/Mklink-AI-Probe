import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref, shallowRef } from 'vue'

const mocks = vi.hoisted(() => ({
  ensureLoaded: vi.fn(),
  refreshStatus: vi.fn(),
  reparse: vi.fn(),
  typeinfo: vi.fn(),
  toastError: vi.fn(),
  loadBrowseChildren: vi.fn(),
  searchSymbols: vi.fn(),
  items: null as any,
  browseRoots: null as any,
  browseChildren: null as any,
  browseLoading: null as any,
  generation: null as any,
}))

const symbolItems = [
  {
    path: 'controller.target', address: 0x20000024, type_name: 'float',
    scalar_kind: 'float', size: 4, writable: true, enum_values: {}, parent_path: 'controller',
  },
  {
    path: 'gain', address: 0x20000020, type_name: 'float',
    scalar_kind: 'float', size: 4, writable: true, enum_values: {}, parent_path: null,
  },
]

vi.mock('../../composables/useSymbolCatalog', () => ({
  useSymbolCatalog: () => ({
    items: mocks.items ??= shallowRef(symbolItems),
    containers: shallowRef([]),
    browseRoots: mocks.browseRoots ??= shallowRef([]),
    browseChildren: mocks.browseChildren ??= shallowRef(new Map()),
    browseLoading: mocks.browseLoading ??= shallowRef(new Set()),
    generation: mocks.generation ??= ref(1),
    stale: ref(false),
    truncatedRoots: shallowRef(['controller']),
    loading: ref(false),
    reparsing: ref(false),
    error: ref(null),
    ensureLoaded: mocks.ensureLoaded,
    refreshStatus: mocks.refreshStatus,
    reparse: mocks.reparse,
    loadBrowseChildren: mocks.loadBrowseChildren,
    searchSymbols: mocks.searchSymbols,
  }),
}))

vi.mock('../../composables/useDashboard', () => ({
  useSymbolsApi: () => ({ typeinfo: mocks.typeinfo }),
}))

vi.mock('../../composables/useToast', () => ({
  useToast: () => ({ error: mocks.toastError, success: vi.fn() }),
}))

import SymbolsTab from './SymbolsTab.vue'

describe('SymbolsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.generation ??= ref(1)
    mocks.generation.value = 1
    mocks.refreshStatus.mockResolvedValue(undefined)
    mocks.items ??= shallowRef(symbolItems)
    mocks.items.value = symbolItems
    mocks.browseRoots ??= shallowRef([])
    mocks.browseRoots.value = [
      {
        key: 'controller', path: 'controller', label: 'controller', kind: 'branch',
        type_name: 'Controller', size: 4, address: 0x20000024,
        descriptor: null, container: null, child_count: 1, range_start: null, range_end: null,
      },
      {
        key: 'gain', path: 'gain', label: 'gain', kind: 'leaf',
        type_name: 'float', size: 4, address: 0x20000020,
        descriptor: symbolItems[1], container: null, child_count: null,
        range_start: null, range_end: null,
      },
    ]
    mocks.browseChildren ??= shallowRef(new Map())
    mocks.browseChildren.value = new Map()
    mocks.browseLoading ??= shallowRef(new Set())
    mocks.browseLoading.value = new Set()
    mocks.ensureLoaded.mockResolvedValue(undefined)
    mocks.typeinfo.mockResolvedValue({
      name: 'gain', found: true, type: 'float', size: 4, address: 0x20000020,
    })
    mocks.searchSymbols.mockImplementation(async (query: string) => symbolItems.filter(item => (
      item.path.includes(query) || item.type_name.includes(query)
    )))
    mocks.loadBrowseChildren.mockImplementation(async (node: { key: string }) => {
      if (node.key !== 'controller') return
      mocks.browseChildren.value = new Map([['controller', [{
        key: 'controller.target', path: 'controller.target', label: 'target', kind: 'leaf',
        type_name: 'float', size: 4, address: 0x20000024,
        descriptor: symbolItems[0], container: null, child_count: null,
        range_start: null, range_end: null,
      }]]])
    })
  })

  it('shows valid catalog variables immediately when opened', async () => {
    const wrapper = mount(SymbolsTab, { props: { deviceConnected: true } })
    await flushPromises()

    expect(mocks.ensureLoaded).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-symbol="controller.target"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('gain')
    expect(wrapper.text()).toContain('controller')
    expect(wrapper.text()).not.toContain('前 256 个')

    await wrapper.get('[data-symbol="controller"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-symbol="controller.target"]').exists()).toBe(true)
  })

  it('refreshes a rebuilt source and cancels polling when closed', async () => {
    vi.useFakeTimers()
    const wrapper = mount(SymbolsTab, { props: { deviceConnected: true } })
    try {
      await flushPromises()
      await wrapper.get('[data-testid="symbol-search"]').setValue('gain')
      await flushPromises()
      await wrapper.get('[data-symbol="gain"]').trigger('click')
      await flushPromises()
      mocks.searchSymbols.mockResolvedValue([{ ...symbolItems[1], address: 0x20000040 }])
      mocks.refreshStatus.mockImplementation(async () => { mocks.generation.value = 2 })
      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(wrapper.text()).toContain('0x20000040')
      expect(wrapper.text()).not.toContain('类型信息:')
      wrapper.unmount()
      const calls = mocks.refreshStatus.mock.calls.length
      await vi.advanceTimersByTimeAsync(4000)
      expect(mocks.refreshStatus).toHaveBeenCalledTimes(calls)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('filters the loaded catalog locally', async () => {
    const wrapper = mount(SymbolsTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="symbol-search"]').setValue('target')

    expect(wrapper.get('[data-symbol="controller.target"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('gain')
    expect(mocks.ensureLoaded).toHaveBeenCalledOnce()
  })

  it('loads type details when a catalog row is selected', async () => {
    const wrapper = mount(SymbolsTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-symbol="gain"]').trigger('click')
    await flushPromises()

    expect(mocks.typeinfo).toHaveBeenCalledWith('gain')
    expect(wrapper.text()).toContain('0x20000020')
  })
})
