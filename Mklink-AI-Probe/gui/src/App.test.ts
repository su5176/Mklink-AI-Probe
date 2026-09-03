import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import { readFileSync } from 'node:fs'
import App from './App.vue'
import VersionHistoryPopover from './components/VersionHistoryPopover.vue'
import { setLanguage } from './composables/useLanguage'

const backendState = ref<'starting' | 'alive' | 'dead'>('starting')
const startStatusPolling = vi.fn()
const restart = vi.fn()
const checkForUpdates = vi.fn()
const installAndRelaunch = vi.fn()
const retryUpdate = vi.fn()
const updateState = ref<'idle' | 'checking' | 'downloading' | 'ready' | 'installing' | 'error'>('idle')
const nativeRuntime = ref(true)
const { startBrowserSessionLease } = vi.hoisted(() => ({
  startBrowserSessionLease: vi.fn(() => vi.fn()),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useRoute: () => ({ name: 'dashboard' }),
}))

vi.mock('./composables/useMklinkApi', () => ({
  useMklinkApi: () => ({ startStatusPolling, stopStatusPolling: vi.fn() }),
}))

vi.mock('./composables/useBackendHealth', () => ({
  useBackendHealth: () => ({
    backendState,
    startHealthPolling: vi.fn(),
    stopHealthPolling: vi.fn(),
    restart,
    isTauri: nativeRuntime.value,
  }),
}))

vi.mock('./composables/useAppUpdater', () => ({
  useAppUpdater: () => ({
    state: updateState,
    version: ref('0.2.0'),
    progress: ref(1),
    error: ref(''),
    checkForUpdates,
    installAndRelaunch,
    retry: retryUpdate,
  }),
}))

vi.mock('./lib/browserSessionLease', () => ({ startBrowserSessionLease }))

function mountApp() {
  return shallowMount(App, {
    global: {
      stubs: {
        StatusBar: true,
        ToastContainer: true,
        AppUpdateBanner: true,
        RouterView: { template: '<div data-testid="route-view" />' },
      },
    },
  })
}

describe('App version footer', () => {
  beforeEach(() => {
    setLanguage('zh')
    nativeRuntime.value = true
  })

  it('switches the global navigation between Chinese and English', async () => {
    const wrapper = mountApp()

    expect(wrapper.get('.app-title').text()).toBe('MKLink')
    expect(wrapper.findAll('.nav-tab').map(tab => tab.text())).toEqual(['配置', '仪表盘', '脱机烧录', '在线烧录', '现场 Agent'])
    await wrapper.get('[data-testid="global-language-toggle"]').trigger('click')
    expect(wrapper.findAll('.nav-tab').map(tab => tab.text())).toEqual(['Config', 'Dashboard', 'Offline Flash', 'Online Flash', 'Site Agent'])
    expect(wrapper.get('[data-testid="global-language-toggle"]').text()).toContain('中文')
    wrapper.unmount()
  })

  it('does not offer native Site Agent configuration in the browser GUI', () => {
    nativeRuntime.value = false
    const wrapper = mountApp()

    expect(wrapper.findAll('.nav-tab').map(tab => tab.text())).not.toContain('现场 Agent')
    wrapper.unmount()
  })

  it('leases the backend only for browser GUI windows', () => {
    startBrowserSessionLease.mockClear()
    nativeRuntime.value = false
    const wrapper = mountApp()

    expect(startBrowserSessionLease).toHaveBeenCalledWith(true)
    wrapper.unmount()
  })

  it('exposes documentation and the ordered store menu without showing raw URLs', () => {
    const wrapper = mountApp()
    const docs = wrapper.get('[data-testid="online-docs-link"]')
    const store = wrapper.get('[data-testid="taobao-link"]')
    const official = wrapper.get('[data-testid="official-store-link"]')
    const xianji = wrapper.get('[data-testid="xianji-store-link"]')
    expect(docs.attributes()).toMatchObject({
      href: 'https://microboot.readthedocs.io/zh-cn/latest/tools/microlink/microlink/',
      target: '_blank',
      rel: 'noopener noreferrer',
    })
    expect(official.attributes()).toMatchObject({
      href: 'https://item.taobao.com/item.htm?ft=t&id=1020501356342',
      target: '_blank',
      rel: 'noopener noreferrer',
    })
    expect(xianji.attributes()).toMatchObject({
      href: 'https://item.taobao.com/item.htm?ft=t&id=1074695414484',
      target: '_blank',
      rel: 'noopener noreferrer',
    })
    expect(docs.text()).toBe('在线文档')
    expect(store.text()).toBe('淘宝店铺')
    expect(official.text()).toBe('官方智沐店铺')
    expect(xianji.text()).toBe('先楫定制店铺')
    expect(xianji.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).not.toContain('microboot.readthedocs.io')
    expect(wrapper.text()).not.toContain('item.taobao.com')
    wrapper.unmount()
  })

  it('checks for desktop updates when the application starts', () => {
    checkForUpdates.mockClear()
    const wrapper = mountApp()

    expect(checkForUpdates).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('shows the stable release and source build in the lower right', () => {
    const wrapper = mountApp()

    expect(wrapper.getComponent(VersionHistoryPopover).props()).toEqual({
      version: __APP_VERSION__,
      buildCommit: expect.stringMatching(/^[0-9a-f]{7,}$/),
    })
    wrapper.unmount()
  })

  it('contains the narrow-window header overflow guard', () => {
    const source = readFileSync('src/App.vue', 'utf8')

    expect(source).toContain('@media (max-width: 720px)')
    expect(source).toContain('grid-template-columns: auto minmax(0, 1fr)')
    expect(source).toContain('.header-right .status-bar')
    expect(source).toContain('width: max-content')
    expect(source).toMatch(/\.nav-tab\s*\{[^}]*white-space:\s*nowrap/s)
    expect(source).toMatch(/@media \(max-width: 720px\)[\s\S]*\.store-menu-panel\s*\{[^}]*position:\s*fixed[^}]*top:\s*46px[^}]*right:\s*8px/s)
  })

  it('does not mount route views or poll device state before the backend API is ready', async () => {
    backendState.value = 'starting'
    startStatusPolling.mockClear()
    const wrapper = mountApp()

    expect(wrapper.find('[data-testid="route-view"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="backend-starting"]').exists()).toBe(true)
    expect(startStatusPolling).not.toHaveBeenCalled()

    backendState.value = 'alive'
    await nextTick()

    expect(wrapper.get('[data-testid="route-view"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="backend-starting"]').exists()).toBe(false)
    expect(startStatusPolling).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('offers sidecar recovery when the first startup attempt fails', async () => {
    backendState.value = 'dead'
    restart.mockClear()
    const wrapper = mountApp()

    expect(wrapper.find('[data-testid="route-view"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="backend-starting"]').exists()).toBe(false)
    await wrapper.get('[data-testid="backend-restart"]').trigger('click')
    expect(restart).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
})
