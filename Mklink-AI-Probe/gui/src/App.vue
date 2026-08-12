<template>
  <div class="app-root">
    <header class="app-header">
      <div class="brand-lockup">
        <span class="company-brand-mark" role="img" aria-label="立芯恒方 ETERNAL CHIP">
          <span class="company-logo-frame" aria-hidden="true">
            <img class="company-logo company-logo-positive" :src="'/brand/eternal-chip-logo-positive.png'" alt="" />
            <img class="company-logo company-logo-negative" :src="'/brand/eternal-chip-logo-negative.png'" alt="" />
          </span>
          <span class="company-name-cn" aria-hidden="true">立芯恒方</span>
        </span>
        <span class="brand-divider" aria-hidden="true"></span>
        <img class="product-device" :src="'/brand/microkeen-probe.png'" alt="" />
        <h1 class="app-title">
          <span>MKLink AI Probe</span>
          <small>MicroKeen · {{ tr('智能调试与烧录工作站', 'Debug & Flash Workstation') }}</small>
        </h1>
      </div>
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
            <button class="store-menu-item store-menu-pending" data-testid="xianji-store-link" type="button" disabled>
              {{ tr('先楫定制店铺（链接待添加）', 'Xianji Custom Store (link pending)') }}
            </button>
          </div>
        </details>
      </nav>
      <div class="header-right">
        <ThemeControl />
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
        <AboutDialog :version="appVersion" :build-commit="buildCommit" />
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
import ThemeControl from './components/ThemeControl.vue'
import AboutDialog from './components/AboutDialog.vue'
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
  --brand: #097e86;
  --brand-hover: #07656b;
  --brand-secondary: #6bb8b1;
  --font-body: 'Alibaba PuHuiTi 2.0', 'Microsoft YaHei UI', 'Microsoft YaHei', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace;
  --radius-control: 5px;
  --radius-card: 8px;
  --radius-dialog: 12px;
  --radius: var(--radius-control);
  --control-height: 36px;
  --shadow-card: 0 8px 24px rgba(5, 63, 67, .07);
  --shadow-control: 0 1px 2px rgba(5, 63, 67, .06);
  --shadow-popover: 0 18px 48px rgba(3, 30, 33, .18);
  --shadow-raised: 0 18px 48px rgba(3, 30, 33, .18);
  --motion-fast: 120ms;
  --motion-base: 200ms;
  --motion-slow: 360ms;
  --ease-standard: cubic-bezier(.2, 0, 0, 1);
  --ease-emphasized: cubic-bezier(.16, 1, .3, 1);
}

:root,
:root[data-theme="porcelain"] {
  --app-bg: #f4f8f8;
  --workspace-bg: #f7fafa;
  --surface: #ffffff;
  --surface-raised: #ffffff;
  --surface-muted: #f4f8f8;
  --surface-selected: #e6f2f3;
  --field-bg: #fbfdfd;
  --text: #333333;
  --text-soft: #526262;
  --muted: #5f7070;
  --dim: #667878;
  --line: #dce6e6;
  --line-strong: #c7d7d7;
  --brand-text: #07656b;
  --focus-ring: #07656b;
  --control-border: #748b8c;
  --nav-active-bg: #d9e9ea;
  --nav-active-text: #054c50;
  --success: #06585e;
  --info: #235486;
  --warn: #765914;
  --danger: #b93232;
  --success-bg: #e6f2f3;
  --info-bg: #e8f1fa;
  --warn-bg: #f7f1df;
  --danger-bg: #faeaea;
  --terminal-bg: #0f171a;
  --terminal-text: #d9e4e4;
}

:root[data-theme="mica"] {
  --app-bg: #e8eded; --workspace-bg: #eef3f3; --surface: #f8fbfb; --surface-raised: #ffffff;
  --surface-muted: #e9eeee; --surface-selected: #dcebec; --field-bg: #f5f8f8;
  --text: #283334; --text-soft: #47595a; --muted: #5c6d6e; --dim: #667879;
  --line: #cbd7d7; --line-strong: #aebfc0; --brand-text: #07656b; --focus-ring: #07656b;
  --control-border: #748b8c; --nav-active-bg: #d9e9ea; --nav-active-text: #054c50;
  --success: #06585e; --info: #235486; --warn: #765914; --danger: #b93232;
  --success-bg: #dcebec; --info-bg: #e3edf5; --warn-bg: #f2ead3; --danger-bg: #f6e2e2;
  --terminal-bg: #11191b; --terminal-text: #d9e4e4;
}

:root[data-theme="aqua"] {
  --app-bg: #e6f2f3; --workspace-bg: #edf7f7; --surface: #faffff; --surface-raised: #ffffff;
  --surface-muted: #e3f0f1; --surface-selected: #cee5e7; --field-bg: #f4fbfb;
  --text: #17393b; --text-soft: #34595b; --muted: #4c696b; --dim: #557274;
  --line: #b5d8db; --line-strong: #84bfc3; --brand-text: #07656b; --focus-ring: #07656b;
  --control-border: #748b8c; --nav-active-bg: #d9e9ea; --nav-active-text: #054c50;
  --success: #06585e; --info: #235486; --warn: #765914; --danger: #b93232;
  --success-bg: #cee5e7; --info-bg: #dceaf6; --warn-bg: #f4ecd3; --danger-bg: #f8e4e4;
  --terminal-bg: #09272a; --terminal-text: #dff0ef;
}

:root[data-theme="abyss"] {
  --app-bg: #0b2529; --workspace-bg: #103136; --surface: #14373c; --surface-raised: #1a4248;
  --surface-muted: #102f34; --surface-selected: #17515a; --field-bg: #0f2c31;
  --text: #edf5f4; --text-soft: #c1d1cf; --muted: #a8bbb8; --dim: #91aaa7;
  --line: rgba(181,216,219,.18); --line-strong: rgba(181,216,219,.30); --brand-text: #9dcbcf; --focus-ring: #84bfc3;
  --control-border: rgba(181,216,219,.48); --nav-active-bg: rgba(107,184,177,.20); --nav-active-text: #edf5f4;
  --success: #b5d8db; --info: #a9cef4; --warn: #e0c87e; --danger: #ff9898;
  --success-bg: rgba(107,184,177,.16); --info-bg: rgba(47,104,168,.22); --warn-bg: rgba(138,106,23,.24); --danger-bg: rgba(194,56,56,.22);
  --terminal-bg: #071c1f; --terminal-text: #e1eceb;
  --shadow-raised: 0 18px 48px rgba(0, 0, 0, .34);
  --shadow-card: 0 10px 28px rgba(0, 0, 0, .20);
  --shadow-control: 0 1px 2px rgba(0, 0, 0, .18);
  --shadow-popover: 0 22px 54px rgba(0, 0, 0, .36);
}

:root[data-theme="graphite"] {
  --app-bg: #0e1415; --workspace-bg: #11191b; --surface: #182224; --surface-raised: #202c2f;
  --surface-muted: #141d1f; --surface-selected: #1b3538; --field-bg: #111a1c;
  --text: #eff4f3; --text-soft: #c7d0cf; --muted: #9caaa9; --dim: #879796;
  --line: rgba(181,216,219,.14); --line-strong: rgba(181,216,219,.26); --brand-text: #9dcbcf; --focus-ring: #84bfc3;
  --control-border: rgba(181,216,219,.48); --nav-active-bg: rgba(107,184,177,.20); --nav-active-text: #edf5f4;
  --success: #b5d8db; --info: #a9cef4; --warn: #e0c87e; --danger: #ff9898;
  --success-bg: rgba(107,184,177,.15); --info-bg: rgba(47,104,168,.20); --warn-bg: rgba(138,106,23,.23); --danger-bg: rgba(194,56,56,.22);
  --terminal-bg: #090d0e; --terminal-text: #e2e8e7;
  --shadow-raised: 0 18px 48px rgba(0, 0, 0, .38);
  --shadow-card: 0 12px 30px rgba(0, 0, 0, .24);
  --shadow-control: 0 1px 2px rgba(0, 0, 0, .22);
  --shadow-popover: 0 24px 58px rgba(0, 0, 0, .42);
}

:root[data-theme="aurora"] {
  --app-bg: #010d0d; --workspace-bg: #02191b; --surface: #032628; --surface-raised: #054c50;
  --surface-muted: #02191b; --surface-selected: #06585e; --field-bg: #032628;
  --text: #f4fbfa; --text-soft: #cee5e7; --muted: #9dcbcf; --dim: #87b5b8;
  --line: rgba(157,203,207,.20); --line-strong: rgba(181,216,219,.38); --brand-text: #9dcbcf; --focus-ring: #84bfc3;
  --control-border: rgba(181,216,219,.48); --nav-active-bg: rgba(107,184,177,.20); --nav-active-text: #edf5f4;
  --success: #b5d8db; --info: #a9cef4; --warn: #e0c87e; --danger: #ff9898;
  --success-bg: rgba(107,184,177,.17); --info-bg: rgba(47,104,168,.24); --warn-bg: rgba(138,106,23,.26); --danger-bg: rgba(194,56,56,.24);
  --terminal-bg: #000909; --terminal-text: #e7f5f3;
  --shadow-raised: 0 18px 52px rgba(0, 0, 0, .46);
  --shadow-card: 0 12px 32px rgba(0, 0, 0, .28);
  --shadow-control: 0 1px 2px rgba(0, 0, 0, .24);
  --shadow-popover: 0 26px 62px rgba(0, 0, 0, .48);
}

:root {
  --bg: var(--app-bg);
  --fg: var(--text);
  --border: var(--line);
  --border-subtle: var(--line);
  --accent: var(--brand-text);
  --accent-light: var(--brand-hover);
  --accent-bg: var(--surface-selected);
  --accent-border: var(--line-strong);
  --ring: var(--line-strong);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--app-bg);
  color: var(--text);
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
  background: color-mix(in srgb, var(--surface) 96%, transparent);
  border-bottom: 1px solid var(--line);
  padding: 0 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
  height: 64px;
  box-shadow: var(--shadow-control);
  backdrop-filter: blur(18px) saturate(112%);
}
.brand-lockup {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 0 0 auto;
  min-width: 0;
}
.company-brand-mark { display: grid; justify-items: start; flex: 0 0 auto; line-height: 1; }
.company-logo-frame { position: relative; width: 112px; height: 27px; display: grid; place-items: center; }
.company-logo { position: absolute; width: 100%; height: 100%; object-fit: contain; }
.company-logo-negative { display: none; }
:root[data-mode="dark"] .company-logo-positive { display: none; }
:root[data-mode="dark"] .company-logo-negative { display: block; }
.company-name-cn { margin: 1px 0 0 2px; color: var(--text-soft); font-size: 10px; font-weight: 650; letter-spacing: .24em; }
.brand-divider { width: 1px; height: 34px; background: var(--line-strong); }
.product-device {
  width: 44px;
  height: 44px;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--surface-muted);
  object-fit: contain;
}
.app-title { display: flex; flex-direction: column; min-width: 0; line-height: 1.15; }
.app-title > span {
  font-size: 16px;
  font-weight: 750;
  color: var(--text);
  letter-spacing: -.01em;
  white-space: nowrap;
}
.app-title small { margin-top: 5px; color: var(--muted); font-size: 10px; font-weight: 500; letter-spacing: .02em; white-space: nowrap; }
.app-title,
.app-title small {
  white-space: nowrap;
}
.app-nav {
  display: flex;
  align-items: center;
  gap: 3px;
}
.nav-tab {
  background: none;
  border: none;
  min-height: var(--control-height);
  padding: 8px 12px;
  border-radius: var(--radius-control);
  font-size: 12px;
  font-weight: 550;
  color: var(--muted);
  cursor: pointer;
  box-shadow: inset 0 -2px 0 transparent;
  transition: color var(--motion-fast), background var(--motion-fast), box-shadow var(--motion-fast);
  font-family: var(--font-body);
  white-space: nowrap;
}
.nav-tab:hover { color: var(--text); background: var(--surface-muted); }
.nav-tab.active {
  color: var(--nav-active-text);
  background: var(--nav-active-bg);
  box-shadow: inset 0 -2px 0 var(--brand-secondary);
  font-weight: 650;
}
.nav-tab:active { box-shadow: inset 0 -2px 0 var(--brand-secondary), inset 0 0 0 1px var(--line); }
.header-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.external-link {
  display: inline-flex;
  align-items: center;
  height: var(--control-height);
  padding: 0 10px;
  color: var(--muted);
  font-size: 12px;
  text-decoration: none;
  white-space: nowrap;
  border-radius: var(--radius-control);
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
  padding: 6px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-card);
  background: var(--surface-raised);
  box-shadow: var(--shadow-popover);
}
.store-menu-item {
  display: block;
  width: 100%;
  min-height: 34px;
  padding: 8px 10px;
  border-radius: var(--radius-control);
  border: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: 12px;
  text-align: left;
  text-decoration: none;
  white-space: nowrap;
}
.store-menu-item:hover { background: var(--surface-selected); color: var(--brand-text); }
.store-menu-pending,
.store-menu-pending:hover { color: var(--dim); cursor: not-allowed; }
.external-link:hover { color: var(--brand-text); background: var(--surface-muted); }
.language-toggle {
  height: var(--control-height);
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 9px;
  border: 1px solid var(--control-border);
  border-radius: var(--radius-control);
  background: var(--surface);
  color: var(--muted);
  font: 500 11px/1 var(--font-body);
  cursor: pointer;
  white-space: nowrap;
  box-shadow: var(--shadow-control);
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard);
}
.language-toggle:hover { color: var(--brand-text); border-color: var(--focus-ring); background: var(--surface-muted); }
button:focus-visible,
a:focus-visible,
summary:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
@media (max-width: 1220px) {
  .app-title small { display: none; }
  .company-logo-frame { width: 96px; }
  .company-name-cn { font-size: 9px; }
  .nav-tab { padding-inline: 9px; }
  .external-link { padding-inline: 7px; }
}
@media (max-width: 1120px) {
  .company-brand-mark, .brand-divider { display: none; }
  .product-device { width: 38px; height: 38px; }
}
@media (max-width: 720px) {
  .app-header {
    height: auto;
    min-height: 60px;
    padding: 0 8px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0 8px;
  }
  .app-nav {
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .app-nav::-webkit-scrollbar { display: none; }
  .nav-tab {
    flex: 0 0 auto;
    padding: 8px;
  }
  .external-link { height: 36px; padding: 0 8px; }
  .store-menu-panel {
    position: fixed;
    top: 58px;
    right: 8px;
  }
  .header-right {
    grid-column: 1 / -1;
    width: 100%;
    min-width: 0;
    margin-left: 0;
    padding-bottom: 6px;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .header-right::-webkit-scrollbar { display: none; }
  .header-right .status-bar {
    width: max-content;
  }
}
.app-main {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 20px;
  background: var(--workspace-bg);
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
  flex: 0 0 24px;
  min-height: 24px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding: 0 12px;
  border-top: 1px solid var(--line);
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
.badge-ok    { background: var(--success-bg); color: var(--success); }
.badge-warn  { background: var(--warn-bg); color: var(--warn); }
.badge-info  { background: var(--info-bg); color: var(--info); }
.badge-err   { background: var(--danger-bg); color: var(--danger); }
.badge-accent { background: var(--surface-selected); color: var(--brand-text); font-weight: 650; }

.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  padding: 20px 24px;
  box-shadow: var(--shadow-card);
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
  height: var(--control-height);
  padding: 0 10px;
  border: 1px solid var(--control-border);
  border-radius: var(--radius-control);
  background: var(--field-bg);
  font-size: 13px;
  color: var(--fg);
  font-family: var(--font-body);
  outline: none;
  box-shadow: var(--shadow-control);
  transition: border-color var(--motion-fast) var(--ease-standard), box-shadow var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard);
}
.form-input:focus, .form-select:focus { border-color: var(--focus-ring); box-shadow: 0 0 0 2px color-mix(in srgb, var(--focus-ring) 16%, transparent); }
.form-select { cursor: pointer; }

.btn {
  height: var(--control-height);
  padding: 0 14px;
  border: 1px solid var(--control-border);
  border-radius: var(--radius-control);
  background: var(--surface);
  font-size: 12px;
  font-weight: 500;
  color: var(--fg);
  cursor: pointer;
  box-shadow: var(--shadow-control);
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard), transform var(--motion-fast) var(--ease-standard), box-shadow var(--motion-fast) var(--ease-standard);
  font-family: var(--font-body);
  white-space: nowrap;
}
.btn:hover { border-color: var(--focus-ring); color: var(--brand-text); transform: translateY(-1px); }
.btn:active { transform: translateY(0); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-primary {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
.btn-primary:hover { background: var(--brand-hover); color: #fff; }
.btn-danger { color: var(--danger); border-color: var(--danger); }
.btn-danger:hover { background: var(--danger); color: #fff; }
.btn-sm { height: 32px; padding: 0 10px; font-size: 11px; }

.btn-group { display: flex; gap: 6px; }

.alert {
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 13px;
  margin-bottom: 12px;
}
.alert-success { background: var(--success-bg); color: var(--success); }
.alert-warn    { background: var(--warn-bg); color: var(--warn); }
.alert-error   { background: var(--danger-bg); color: var(--danger); }
.alert-info    { background: var(--info-bg); color: var(--info); }

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
  background: var(--surface-muted);
  border: 1px solid var(--line);
}
.desc-table td {
  padding: 6px 12px;
  border: 1px solid var(--line);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 12px;
  word-break: break-all;
}

.tabs-bar {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--line);
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
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard);
  font-family: var(--font-body);
  white-space: nowrap;
}
.tab-btn:hover { color: var(--fg); }
.tab-btn.active { color: var(--brand-text); border-bottom-color: var(--brand); font-weight: 650; }

pre.log-box {
  background: var(--terminal-bg);
  color: var(--terminal-text);
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

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
  .btn:hover { transform: none; }
}

@media (max-width: 720px) {
  .app-main { padding: 12px; }
  .card { padding: 16px; }
}
</style>
