import { createApp } from 'vue'
import { initializeRuntimeEndpoint } from './lib/runtimeEndpoint'

type StartupController = {
  update: (value: number, status?: string) => void
  finish: () => void
}

declare global {
  interface Window {
    __MKLINK_STARTUP__?: StartupController
  }
}

function startupStatus(chinese: string, english: string): string {
  return localStorage.getItem('mklink_lang') === 'en' ? english : chinese
}

function updateStartup(value: number, chinese: string, english: string): void {
  window.__MKLINK_STARTUP__?.update(value, startupStatus(chinese, english))
}

async function startApp() {
  updateStartup(22, '正在启动本地服务…', 'Starting local service…')
  let endpointProgress = 22
  const endpointTimer = window.setInterval(() => {
    endpointProgress = Math.min(74, endpointProgress + 1)
    updateStartup(endpointProgress, '正在启动本地服务…', 'Starting local service…')
  }, 180)
  try {
    await initializeRuntimeEndpoint()
  } catch (error) {
    console.error('[main] backend endpoint initialization failed:', error)
  } finally {
    window.clearInterval(endpointTimer)
  }
  updateStartup(80, '本地服务已就绪', 'Local service is ready')
  const [{ default: App }, { default: router }] = await Promise.all([
    import('./App.vue'),
    import('./router'),
  ])
  updateStartup(94, '正在加载工作区…', 'Loading workspace…')
  createApp(App).use(router).mount('#app')
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => window.__MKLINK_STARTUP__?.finish())
  })
}

void startApp()
