<template>
  <div class="app-root">
    <header class="app-header">
      <h1 class="app-title">MKLink</h1>
      <nav class="app-nav">
        <button
          v-for="tab in tabs" :key="tab.key"
          :class="['nav-tab', { active: currentTab === tab.key }]"
          @click="navigate(tab.key)"
        >{{ tab.label }}</button>
        <a
          class="external-link"
          data-testid="online-docs-link"
          href="https://microboot.readthedocs.io/zh-cn/latest/tools/microlink/microlink/"
          target="_blank"
          rel="noopener noreferrer"
        >{{ tr('在线文档', 'Docs') }}</a>
        <details class="store-menu" data-testid="taobao-menu">
          <summary class="external-link" data-testid="taobao-link">{{ tr('淘宝店铺', 'Stores') }}</summary>
          <div class="store-menu-panel">
            <a
              class="store-menu-item"
              data-testid="official-store-link"
              href="https://item.taobao.com/item.htm?ft=t&id=1020501356342"
              target="_blank"
              rel="noopener noreferrer"
            >{{ tr('官方智沐店铺', 'Official Zhi Mu Store') }}</a>
            <a
              class="store-menu-item"
              data-testid="xianji-store-link"
              href="https://item.taobao.com/item.htm?ft=t&id=1074695414484"
              target="_blank"
              rel="noopener noreferrer"
            >{{ tr('先楫定制店铺', 'Xianji Custom Store') }}</a>
          </div>
        </details>
      </nav>
      <div class="header-right">
        <button
          class="language-toggle"
          type="button"
          data-testid="global-language-toggle"
          :title="tr('切换到 English', 'Switch to Chinese')"
          :aria-label="tr('切换到 English', 'Switch to Chinese')"
          @click="toggleLanguage"
        >
          <Languages :size="15" aria-hidden="true" />
          <span>{{ language === 'zh' ? 'EN' : '中文' }}</span>
        </button>
        <StatusBar />
      </div>
    </header>
    <AppUpdateBanner
      v-if="!updateDismissed"
      :state="updateState"
      :version="updateVersion"
      :progress="updateProgress"
      :error="updateError"
      @install="installAndRelaunch"
      @retry="retryUpdate"
      @dismiss="updateDismissed = true"
    />
    <div class="app-main">
      <router-view v-if="initialBackendReady" v-slot="{ Component }">
        <KeepAlive include="OnlineFlashView,OfflineFlashView">
          <component :is="Component" />
        </KeepAlive>
      </router-view>
      <div v-else-if="backendState === 'starting'" class="backend-starting" data-testid="backend-starting" role="status">
        {{ tr('正在启动本地服务…', 'Starting local service…') }}
      </div>
      <div v-else class="backend-recovery" role="alert">
        <strong>{{ tr('本地服务未启动', 'Local service is not running') }}</strong>
        <button data-testid="backend-restart" @click="restart">{{ tr('重启服务', 'Restart Service') }}</button>
      </div>
    </div>
    <footer class="app-footer">
      <VersionHistoryPopover :version="appVersion" :build-commit="buildCommit" />
    </footer>
    <ToastContainer />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Languages } from '@lucide/vue'
import StatusBar from './components/StatusBar.vue'
import ToastContainer from './components/ToastContainer.vue'
import AppUpdateBanner from './components/AppUpdateBanner.vue'
import VersionHistoryPopover from './components/VersionHistoryPopover.vue'
import { useMklinkApi } from './composables/useMklinkApi'
import { useBackendHealth } from './composables/useBackendHealth'
import { useAppUpdater } from './composables/useAppUpdater'
import { language, toggleLanguage, tr } from './composables/useLanguage'
import { startBrowserSessionLease } from './lib/browserSessionLease'

const router = useRouter()
const route = useRoute()
const { startStatusPolling, stopStatusPolling } = useMklinkApi()
const { backendState, startHealthPolling, stopHealthPolling, restart, isTauri } = useBackendHealth()
const {
  state: updateState,
  version: updateVersion,
  progress: updateProgress,
  error: updateError,
  checkForUpdates,
  installAndRelaunch,
  retry: retryUpdate,
} = useAppUpdater()
const initialBackendReady = ref(false)
const updateDismissed = ref(false)
let statusPollingStarted = false
let stopBrowserSessionLease: () => void = () => undefined
const appVersion = __APP_VERSION__
const buildCommit = __APP_BUILD_COMMIT__

const currentTab = computed(() => route.name as string)

const tabs = computed(() => [
  { key: 'config', label: tr('配置', 'Config') },
  { key: 'dashboard', label: tr('仪表盘', 'Dashboard') },
  { key: 'offline-flash', label: tr('脱机烧录', 'Offline Flash') },
  { key: 'online-flash', label: tr('在线烧录', 'Online Flash') },
  { key: 'site-agent', label: tr('现场 Agent', 'Site Agent') },
].filter(entry => entry.key !== 'site-agent' || isTauri))

function navigate(key: string) {
  router.push({ name: key })
}

watch(backendState, state => {
  if (state !== 'alive' || initialBackendReady.value) return
  initialBackendReady.value = true
  if (!statusPollingStarted) {
    statusPollingStarted = true
    startStatusPolling(3000)
  }
}, { immediate: true })

onMounted(() => {
  stopBrowserSessionLease = startBrowserSessionLease(!isTauri)
  startHealthPolling(5000)
  void checkForUpdates()
})
onUnmounted(() => {
  if (statusPollingStarted) stopStatusPolling()
  stopHealthPolling()
  stopBrowserSessionLease()
})
</script>

<style>
:root {
  --bg:      #f5f4ed;
  --surface: #faf9f5;
  --fg:      #141413;
  --muted:   #5e5d59;
  --dim:     #87867f;
  --border:  #e8e6dc;
  --border-subtle: #f0eee6;
  --accent:  #c96442;
  --accent-light: #d97757;
  --info:    #3898ec;
  --danger:  #b53333;
  --warn:    #b58a1b;
  --success: #2d6a4f;
  --font-body: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --font-mono: Consolas, 'JetBrains Mono', ui-monospace, Menlo, monospace;
  --radius: 6px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.5;
}
.app-root {
  height: 100vh;
  display: flex;
  flex-direction: column;
}
.app-header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
  height: 48px;
}
.app-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: 0;
  white-space: nowrap;
}
.app-nav {
  display: flex;
  gap: 2px;
}
.nav-tab {
  background: none;
  border: none;
  padding: 12px 18px;
  font-size: 13px;
  font-weight: 500;
  color: var(--muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
  font-family: var(--font-body);
  white-space: nowrap;
}
.nav-tab:hover { color: var(--fg); border-bottom-color: var(--border); }
.nav-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}
.header-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.external-link {
  display: inline-flex;
  align-items: center;
  height: 44px;
  padding: 0 10px;
  color: var(--muted);
  font-size: 12px;
  text-decoration: none;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
}
.store-menu {
  position: relative;
}
.store-menu > summary {
  list-style: none;
  cursor: pointer;
}
.store-menu > summary::-webkit-details-marker { display: none; }
.store-menu-panel {
  position: absolute;
  z-index: 30;
  top: calc(100% - 2px);
  right: 0;
  display: grid;
  min-width: 190px;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: 0 8px 20px rgba(20, 20, 19, 0.12);
}
.store-menu-item {
  display: block;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: 12px;
  text-align: left;
  text-decoration: none;
  white-space: nowrap;
}
.store-menu-item:hover { background: var(--border-subtle); color: var(--accent); }
.store-menu-pending,
.store-menu-pending:hover { color: var(--dim); cursor: not-allowed; }
.external-link:hover { color: var(--accent); border-bottom-color: var(--border); }
.language-toggle {
  height: 28px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--muted);
  font: 500 11px/1 var(--font-body);
  cursor: pointer;
  white-space: nowrap;
}
.language-toggle:hover { color: var(--accent); border-color: var(--accent); }
@media (max-width: 720px) {
  .app-header {
    height: auto;
    min-height: 48px;
    padding: 0 8px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0 8px;
  }
  .app-nav {
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: thin;
  }
  .nav-tab {
    flex: 0 0 auto;
    padding: 12px 8px;
  }
  .external-link { height: 44px; padding: 0 8px; }
  .store-menu-panel {
    position: fixed;
    top: 46px;
    right: 8px;
  }
  .header-right {
    grid-column: 1 / -1;
    width: 100%;
    min-width: 0;
    margin-left: 0;
    padding-bottom: 6px;
    overflow-x: auto;
    scrollbar-width: thin;
  }
  .header-right .status-bar {
    width: max-content;
  }
}
.app-main {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 20px;
}
.backend-starting {
  height: 100%;
  display: grid;
  place-items: center;
  color: var(--muted);
  font-size: 13px;
}
.backend-recovery {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--danger);
}
.backend-recovery button {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--fg);
  padding: 6px 12px;
  cursor: pointer;
}
.app-footer {
  flex: 0 0 22px;
  min-height: 22px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding: 0 12px;
  border-top: 1px solid var(--border-subtle);
  background: var(--surface);
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 10px;
}

/* ---- shared components ---- */
.badge {
  font-size: 11px;
  font-weight: 500;
  padding: 3px 10px;
  border-radius: 100px;
  letter-spacing: 0.02em;
  display: inline-block;
}
.badge-ok    { background: #e6f2ea; color: var(--success); }
.badge-warn  { background: #f5f0e1; color: var(--warn); }
.badge-info  { background: #e6eef5; color: var(--info); }
.badge-err   { background: #f5e6e6; color: var(--danger); }
.badge-accent { background: #f3ece6; color: var(--accent); font-weight: 600; }

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 20px;
}
.card + .card { margin-top: 16px; }
.card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--fg);
}

.form-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.form-label {
  width: 100px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--muted);
  text-align: right;
}
.form-input, .form-select {
  flex: 1;
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: #fff;
  font-size: 13px;
  color: var(--fg);
  font-family: var(--font-body);
  outline: none;
  transition: border-color 0.15s;
}
.form-input:focus, .form-select:focus { border-color: var(--accent); }
.form-select { cursor: pointer; }

.btn {
  height: 30px;
  padding: 0 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  font-size: 12px;
  font-weight: 500;
  color: var(--fg);
  cursor: pointer;
  transition: all 0.15s;
  font-family: var(--font-body);
  white-space: nowrap;
}
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.btn-primary:hover { background: var(--accent-light); color: #fff; }
.btn-danger { color: var(--danger); border-color: var(--danger); }
.btn-danger:hover { background: var(--danger); color: #fff; }
.btn-sm { height: 26px; padding: 0 10px; font-size: 11px; }

.btn-group { display: flex; gap: 6px; }

.alert {
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 13px;
  margin-bottom: 12px;
}
.alert-success { background: #e6f2ea; color: var(--success); }
.alert-warn    { background: #f5f0e1; color: var(--warn); }
.alert-error   { background: #f5e6e6; color: var(--danger); }
.alert-info    { background: #e6eef5; color: var(--info); }

.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
@media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

.desc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.desc-table th {
  text-align: left;
  font-weight: 500;
  color: var(--muted);
  padding: 6px 12px;
  background: var(--bg);
  border: 1px solid var(--border);
}
.desc-table td {
  padding: 6px 12px;
  border: 1px solid var(--border);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 12px;
  word-break: break-all;
}

.tabs-bar {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
  overflow-x: auto;
  scrollbar-width: thin;
}
.tab-btn {
  flex: 0 0 auto;
  background: none;
  border: none;
  padding: 8px 18px;
  font-size: 13px;
  color: var(--muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
  font-family: var(--font-body);
  white-space: nowrap;
}
.tab-btn:hover { color: var(--fg); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }

pre.log-box {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: var(--radius);
  font-family: var(--font-mono);
  font-size: 12px;
  max-height: 400px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.6;
}
</style>
