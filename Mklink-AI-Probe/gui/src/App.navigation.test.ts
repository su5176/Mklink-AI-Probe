import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from './App.vue'

vi.mock('./composables/useMklinkApi', () => ({
  useMklinkApi: () => ({ startStatusPolling: vi.fn(), stopStatusPolling: vi.fn() }),
}))
vi.mock('./composables/useBackendHealth', () => ({
  useBackendHealth: () => ({ backendState: ref('alive'), startHealthPolling: vi.fn(),
    stopHealthPolling: vi.fn(), restart: vi.fn(), isTauri: false }),
}))
vi.mock('./composables/useAppUpdater', () => ({
  useAppUpdater: () => ({ state: ref('idle'), version: ref(''), progress: ref(0),
    error: ref(''), checkForUpdates: vi.fn(), installAndRelaunch: vi.fn(), retry: vi.fn() }),
}))
vi.mock('./lib/browserSessionLease', () => ({ startBrowserSessionLease: () => vi.fn() }))
vi.mock('./views/DashboardView.vue', () => ({
  __esModule: true,
  default: { template: '<div data-testid="dashboard-page">Dashboard</div>' },
}))

// Keep real RouterView and KeepAlive: shallow stubs cannot detect cache collisions.
function page(name: string, id: string) {
  return defineComponent({ name, setup: () => ({ value: ref('') }),
    template: `<section data-testid="${id}"><input v-model="value" /></section>` })
}

describe('App route cache', () => {
  it.each(['online-flash', 'offline-flash'])('keeps pages and edits separate starting at %s', async first => {
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/online-flash', name: 'online-flash', component: async () => page('OnlineFlashView', 'online-page') },
      { path: '/offline-flash', name: 'offline-flash', component: async () => page('OfflineFlashView', 'offline-page') },
      { path: '/config', name: 'config', component: page('ConfigView', 'config-page') },
      { path: '/dashboard', name: 'dashboard', component: { template: '<div />' } },
    ] })
    await router.push('/' + first)
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router], stubs: {
      StatusBar: true, ToastContainer: true, AppUpdateBanner: true, VersionHistoryPopover: true,
    } } })
    try {
      for (const name of [first, first === 'online-flash' ? 'offline-flash' : 'online-flash']) {
        await router.push({ name })
        await flushPromises()
        await wrapper.get(`[data-testid="${name.replace('-flash', '-page')}"] input`).setValue(name)
      }
      const labels: Record<string, string> = {
        dashboard: '仪表盘', config: '配置', 'online-flash': '在线烧录', 'offline-flash': '脱机烧录',
      }
      for (const name of ['dashboard', 'offline-flash', 'online-flash', 'config', 'online-flash', 'offline-flash', 'dashboard', 'offline-flash']) {
        await wrapper.findAll('.nav-tab').find(button => button.text() === labels[name])!.trigger('click')
        await flushPromises()
        expect(router.currentRoute.value.name).toBe(name)
        if (name.endsWith('-flash')) {
          const id = name.replace('-flash', '-page')
          expect(wrapper.get(`[data-testid="${id}"]`).isVisible()).toBe(true)
          expect((wrapper.get(`[data-testid="${id}"] input`).element as HTMLInputElement).value).toBe(name)
          expect(wrapper.find(`[data-testid="${id === 'online-page' ? 'offline-page' : 'online-page'}"]`).exists()).toBe(false)
        }
        if (name === 'dashboard') expect(wrapper.get('[data-testid="dashboard-page"]').isVisible()).toBe(true)
      }
      expect(wrapper.get('[data-testid="dashboard-page"]').attributes('style')).toContain('display: none')
    } finally { wrapper.unmount() }
  })
})
