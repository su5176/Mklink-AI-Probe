<script setup lang="ts">
import { computed, reactive, ref, onMounted } from 'vue'
import { Download, RefreshCw, Search, TriangleAlert, Unplug, Usb } from '@lucide/vue'
import { useMklinkApi } from '../composables/useMklinkApi'
import { useMklinkWs } from '../composables/useMklinkWs'
import { useToast } from '../composables/useToast'
import { useSymbolCatalog } from '../composables/useSymbolCatalog'
import { tr } from '../composables/useLanguage'
import {
  isSameFileSourcePath,
  isSymbolFilePath,
  loadDesktopSettings,
  saveDesktopSettings,
  type DesktopSettings,
} from '../lib/desktopSettings'
import { pickMapFile, pickSymbolFile, type PickedFile } from '../lib/filePicker'
import { saveBlobFile } from '../lib/downloadTextFile'
import type { AxlStatus, FileSourceKind, PortInfo, ProbeFirmwareCheck, ProbeFirmwareUpgrade, ProjectConfig } from '../types/mklink'
import ConfigSectionNav, { type ConfigSection } from '../components/config/ConfigSectionNav.vue'
import FileSourcesPanel from '../components/config/FileSourcesPanel.vue'
import FirmwareUpdateModal from '../components/config/FirmwareUpdateModal.vue'

const {
  deviceStatus,
  listPorts,
  discoverPort,
  getConfig,
  updateConfig,
  uploadFileSource,
  connectDevice,
  disconnectDevice,
  parseAxf,
  probeFirmwareCheck,
  upgradeProbeFirmware,
  downloadProbeFirmware,
} = useMklinkApi()
const { wsConnected, connect: wsConnect, disconnect: wsDisconnect } = useMklinkWs()
const toast = useToast()
const symbolCatalog = useSymbolCatalog()

const activeSection = ref<ConfigSection>('local')
const config = ref<ProjectConfig>({})
const localPort = ref('')
const portOptions = ref<{ label: string; value: string }[]>([])
const localPortExplicit = ref(false)
const settings = ref<DesktopSettings>(loadDesktopSettings(window.localStorage))

const portsLoading = ref(false)
const savingLocal = ref(false)
const connecting = ref(false)
const disconnecting = ref(false)
const browsingFiles = ref(false)
const parsingSymbols = ref(false)
const localSaveState = ref<'idle' | 'saving' | 'saved'>('idle')

const remoteUrl = ref('ws://127.0.0.1:8765')
const remoteToken = ref('')
const wsConnecting = ref(false)
const serveConfig = reactive({ host: '127.0.0.1', port: 8765, token: '' })
const launching = ref(false)

const firmwareCheck = ref<ProbeFirmwareCheck | null>(null)
const showFirmwareModal = ref(false)
const firmwareUpgrading = ref(false)
const firmwareUpgradeStatus = ref('')
const firmwareUpgradeResult = ref<ProbeFirmwareUpgrade | null>(null)
const firmwareDownloading = ref(false)
const firmwareDownloadStatus = ref('')
const manualFirmwareUpgrade = computed(() => {
  const result = firmwareUpgradeResult.value
  if (!result || result.status === 'updated' || result.status === 'up_to_date') return null
  return result.download_available && result.latest_version && result.firmware && result.model
    ? result
    : null
})

async function refreshPorts() {
  portsLoading.value = true
  try {
    const ports: PortInfo[] = await listPorts()
    portOptions.value = ports.map(port => ({
      label: `${port.device} — ${port.description} (${port.manufacturer})`,
      value: port.device,
    }))
  } catch (error: any) {
    toast.error(tr('读取串口失败: ', 'Failed to read serial ports: ') + error.message)
  } finally {
    portsLoading.value = false
  }
}

async function autoDiscover() {
  portsLoading.value = true
  try {
    const result = await discoverPort()
    if (result.port) {
      localPort.value = result.port
      localPortExplicit.value = false
      await saveLocalConfig()
    }
  } catch (error: any) {
    toast.error(tr('自动检测失败: ', 'Auto-detection failed: ') + error.message)
  } finally {
    portsLoading.value = false
  }
}

async function loadConfig() {
  try {
    config.value = await getConfig()
    localPort.value = config.value.com_port || ''
    localPortExplicit.value = false
  } catch (error: any) {
    toast.error(tr('读取配置失败: ', 'Failed to load configuration: ') + error.message)
  }
}

async function saveLocalConfig() {
  const rawClock = String(config.value.swd_clock ?? '').trim()
  if (rawClock) {
    const clock = Number(rawClock)
    if (!Number.isInteger(clock) || clock < 1 || clock > 10_000_000) {
      toast.error(tr('SWD 时钟必须是 1 Hz 到 10 MHz 之间的整数', 'SWD clock must be an integer from 1 Hz to 10 MHz'))
      return
    }
  }
  savingLocal.value = true
  localSaveState.value = 'saving'
  try {
    config.value = await updateConfig({
      ...config.value,
      com_port: localPort.value.trim(),
      swd_clock: rawClock || undefined,
    })
    localSaveState.value = 'saved'
  } catch (error: any) {
    localSaveState.value = 'idle'
    toast.error(tr('保存配置失败: ', 'Failed to save configuration: ') + error.message)
  } finally {
    savingLocal.value = false
  }
}

async function selectLocalPort() {
  localPortExplicit.value = Boolean(localPort.value.trim())
  await saveLocalConfig()
}

async function connectLocal() {
  connecting.value = true
  try {
    const selectedPort = localPort.value.trim()
    const result = await connectDevice({
      ...(localPortExplicit.value && selectedPort
        ? { port: selectedPort }
        : { restore_last: true }),
      axf: isSymbolFilePath(settings.value.symbolPath)
        ? settings.value.symbolPath.trim()
        : undefined,
    })
    const connectedPort = result.port || deviceStatus.value.port
    if (connectedPort) {
      localPort.value = connectedPort
      localPortExplicit.value = false
      config.value = { ...config.value, com_port: connectedPort }
    }
  } catch (error: any) {
    localPort.value = ''
    localPortExplicit.value = false
    config.value = { ...config.value, com_port: '' }
    toast.error(tr('连接失败: ', 'Connection failed: ') + error.message)
  } finally {
    connecting.value = false
  }
}

async function disconnectLocal() {
  disconnecting.value = true
  try {
    await disconnectDevice()
  } catch (error: any) {
    toast.error(tr('断开失败: ', 'Disconnect failed: ') + error.message)
  } finally {
    disconnecting.value = false
  }
}

interface SelectedFileSource {
  path: string
  displayPath: string
}

async function selectedFilePath(
  kind: FileSourceKind,
  selected: PickedFile,
): Promise<SelectedFileSource | null> {
  if (!selected) return null
  if (typeof selected === 'string') return { path: selected, displayPath: selected }
  const uploaded = await uploadFileSource(kind, selected)
  return { path: uploaded.path, displayPath: uploaded.name || selected.name }
}

async function browseSymbolFile() {
  browsingFiles.value = true
  try {
    const source = await selectedFilePath('symbol', await pickSymbolFile())
    if (source) updateFilePath('symbol', source.path, source.displayPath)
  } catch (error: any) {
    toast.error(tr('加载 AXF / ELF 文件失败: ', 'Failed to load AXF / ELF file: ') + error.message)
  } finally {
    browsingFiles.value = false
  }
}

async function browseMapFile() {
  browsingFiles.value = true
  try {
    const source = await selectedFilePath('map', await pickMapFile())
    if (source) updateFilePath('map', source.path, source.displayPath)
  } catch (error: any) {
    toast.error(tr('加载 MAP 文件失败: ', 'Failed to load MAP file: ') + error.message)
  } finally {
    browsingFiles.value = false
  }
}

function persistFilePaths() {
  try {
    saveDesktopSettings(window.localStorage, settings.value)
  } catch (error: any) {
    toast.error(tr('保存文件路径失败: ', 'Failed to save file paths: ') + error.message)
  }
}

function updateFilePath(kind: 'symbol' | 'map', value: string, displayPath = value) {
  if (kind === 'symbol') {
    settings.value.symbolPath = value
    settings.value.symbolDisplayPath = displayPath === value ? '' : displayPath
  } else {
    settings.value.mapPath = value
    settings.value.mapDisplayPath = displayPath === value ? '' : displayPath
  }
  persistFilePaths()
}

async function parseSymbols() {
  if (!deviceStatus.value.connected || !isSymbolFilePath(settings.value.symbolPath)) return
  parsingSymbols.value = true
  try {
    const requestedPath = settings.value.symbolPath.trim()
    const result = await parseAxf(requestedPath) as AxlStatus
    if (result.loaded) {
      if (!isSameFileSourcePath(requestedPath, result.axf_path)) {
        toast.error(tr(`AXF 解析失败: 后端仍在使用 ${result.axf_path || '未知文件'}`, `AXF parsing failed: backend is still using ${result.axf_path || 'an unknown file'}`))
        return
      }
      try {
        await symbolCatalog.ensureLoaded(true)
      } catch (error: any) {
        toast.error(tr('符号目录刷新失败: ', 'Failed to refresh symbol catalog: ') + error.message)
        return
      }
      toast.success(tr(`AXF 解析成功: ${result.variable_count || 0} 个固定可读变量`, `AXF parsed: ${result.variable_count || 0} fixed readable variables`))
    } else {
      toast.error(tr('AXF 解析失败', 'AXF parsing failed'))
    }
  } catch (error: any) {
    toast.error(tr('AXF 解析失败: ', 'AXF parsing failed: ') + error.message)
  } finally {
    parsingSymbols.value = false
  }
}

function connectRemote() {
  wsConnecting.value = true
  try {
    wsConnect(remoteToken.value || undefined, remoteUrl.value || undefined)
  } finally {
    wsConnecting.value = false
  }
}

function launchServer() {
  launching.value = true
  window.open(`http://${serveConfig.host}:${serveConfig.port}/docs`, '_blank')
  launching.value = false
}

async function recheckFirmware(openModal = true) {
  try {
    firmwareCheck.value = await probeFirmwareCheck()
    if (openModal && firmwareCheck.value.status === 'upgrade_required') {
      showFirmwareModal.value = true
    }
  } catch {
    // Firmware checks are advisory and must not block configuration.
  }
}

async function upgradeFirmware() {
  if (firmwareUpgrading.value || !deviceStatus.value.connected) return
  if (!window.confirm(tr(
    '将让探针进入 Bootloader 并重启连接，随后自动复制 UF2 固件。继续吗？',
    'The probe will enter Bootloader and restart its connection before copying the UF2 firmware. Continue?',
  ))) return
  firmwareUpgrading.value = true
  firmwareUpgradeResult.value = null
  firmwareDownloadStatus.value = ''
  firmwareUpgradeStatus.value = tr('正在升级探针固件...', 'Upgrading probe firmware...')
  try {
    const result = await upgradeProbeFirmware(true)
    firmwareUpgradeResult.value = result
    if (result.status === 'updated') {
      firmwareUpgradeStatus.value = tr(
        `升级完成：${result.verified_version || result.latest_version || ''}`,
        `Update complete: ${result.verified_version || result.latest_version || ''}`,
      )
      toast.success(firmwareUpgradeStatus.value)
    } else if (result.status === 'up_to_date') {
      firmwareUpgradeStatus.value = tr('当前已是最新固件', 'The probe firmware is already up to date')
      toast.success(firmwareUpgradeStatus.value)
    } else {
      firmwareUpgradeStatus.value = result.message || tr('未完成自动升级，请按提示手动升级', 'Automatic update did not complete; follow the manual update instructions')
      toast.error(firmwareUpgradeStatus.value)
    }
    await recheckFirmware(false)
  } catch (error: any) {
    firmwareUpgradeStatus.value = error?.message || tr('固件升级失败', 'Firmware update failed')
    toast.error(firmwareUpgradeStatus.value)
  } finally {
    firmwareUpgrading.value = false
  }
}

async function downloadFirmware() {
  const result = manualFirmwareUpgrade.value
  if (!result?.model || firmwareDownloading.value) return
  firmwareDownloading.value = true
  firmwareDownloadStatus.value = tr('正在下载固件...', 'Downloading firmware...')
  try {
    const downloaded = await downloadProbeFirmware(
      result.model,
      result.family || 'microlink',
    )
    const saved = await saveBlobFile(downloaded.filename, downloaded.blob)
    if (!saved) {
      firmwareDownloadStatus.value = ''
      return
    }
    const source = downloaded.source === 'gitee'
      ? tr('Gitee', 'Gitee')
      : downloaded.source === 'local'
        ? tr('本地固件包', 'local firmware package')
        : tr('GitHub', 'GitHub')
    firmwareDownloadStatus.value = tr(
      `已保存 ${downloaded.filename}（来源：${source}）`,
      `Saved ${downloaded.filename} (source: ${source})`,
    )
    toast.success(firmwareDownloadStatus.value)
  } catch (error: any) {
    firmwareDownloadStatus.value = error?.message || tr('固件下载失败', 'Firmware download failed')
    toast.error(firmwareDownloadStatus.value)
  } finally {
    firmwareDownloading.value = false
  }
}

onMounted(async () => {
  await Promise.all([refreshPorts(), loadConfig(), recheckFirmware(false)])
  const restoredPort = localPort.value.trim()
  if (restoredPort && !portOptions.value.some(option => option.value === restoredPort)) {
    localPort.value = ''
  }
})
</script>

<template>
  <div class="config-workspace">
    <ConfigSectionNav v-model="activeSection" />

    <main class="section-content">
      <section
        v-if="activeSection === 'local'"
        class="card local-panel"
        data-testid="local-device-panel"
        aria-labelledby="local-device-title"
      >
        <header class="config-panel-header">
          <div class="config-panel-heading-copy">
            <span class="config-panel-eyebrow">MICROKEEN PROBE</span>
            <h2 id="local-device-title">{{ tr('本地设备', 'Local Device') }}</h2>
            <p>{{ tr('选择调试器串口并配置 SWD 调试链路。', 'Select the probe port and configure the SWD debug link.') }}</p>
          </div>
          <span :class="['badge', deviceStatus.connected ? 'badge-ok' : 'badge-err']">
            {{ deviceStatus.connected ? tr('已连接', 'Connected') : tr('未连接', 'Disconnected') }}
          </span>
        </header>

        <div class="form-row">
          <label class="form-label" for="local-port">{{ tr('串口', 'Serial Port') }}</label>
          <select id="local-port" v-model="localPort" class="form-select" data-testid="local-port" @change="selectLocalPort">
            <option value="">{{ tr('自动搜索', 'Auto Search') }}</option>
            <option v-for="port in portOptions" :key="port.value" :value="port.value">
              {{ port.label }}
            </option>
          </select>
          <button
            class="btn btn-sm icon-button"
            type="button"
            :title="tr('刷新串口', 'Refresh serial ports')"
            data-testid="refresh-ports"
            :disabled="portsLoading"
            @click="refreshPorts"
          >
            <RefreshCw :size="14" aria-hidden="true" />
          </button>
          <button
            class="btn btn-sm icon-command"
            type="button"
            data-testid="auto-port"
            :disabled="portsLoading"
            @click="autoDiscover"
          >
            <Search :size="14" aria-hidden="true" />
            {{ tr('自动', 'Auto') }}
          </button>
        </div>

        <div class="form-row">
          <label class="form-label" for="swd-clock">{{ tr('SWD 时钟', 'SWD Clock') }}</label>
          <input
            id="swd-clock"
            v-model="config.swd_clock"
            type="number"
            min="1"
            max="10000000"
            step="1"
            class="form-input"
            data-testid="swd-clock"
            :placeholder="tr('如 1000000', 'e.g. 1000000')"
            @change="saveLocalConfig"
          />
        </div>

        <div class="local-actions">
          <span class="auto-save-state" data-testid="local-auto-save">
            {{ localSaveState === 'saving' ? tr('自动保存中...', 'Saving...') : localSaveState === 'saved' ? tr('已自动保存', 'Saved') : tr('修改后自动保存', 'Changes save automatically') }}
          </span>
          <button
            class="btn btn-primary icon-command"
            type="button"
            data-testid="connect-local"
            :disabled="connecting || deviceStatus.connected"
            @click="connectLocal"
          >
            <Usb :size="15" aria-hidden="true" />
            {{ connecting ? tr('连接中...', 'Connecting...') : tr('连接设备', 'Connect Device') }}
          </button>
          <button
            class="btn icon-command"
            type="button"
            data-testid="disconnect-local"
            :disabled="disconnecting || !deviceStatus.connected"
            @click="disconnectLocal"
          >
            <Unplug :size="15" aria-hidden="true" />
            {{ disconnecting ? tr('断开中...', 'Disconnecting...') : tr('断开', 'Disconnect') }}
          </button>
        </div>

      </section>

      <FileSourcesPanel
        v-else-if="activeSection === 'files'"
        :symbol-path="settings.symbolPath"
        :symbol-display-path="settings.symbolDisplayPath"
        :map-path="settings.mapPath"
        :map-display-path="settings.mapDisplayPath"
        :connected="deviceStatus.connected"
        :symbol-status="deviceStatus.axf"
        :browsing="browsingFiles"
        :parsing="parsingSymbols"
        @update:symbol-path="updateFilePath('symbol', $event)"
        @update:map-path="updateFilePath('map', $event)"
        @browse-symbol="browseSymbolFile"
        @browse-map="browseMapFile"
        @parse="parseSymbols"
      />

      <section v-else-if="activeSection === 'remote'" class="card remote-panel">
        <header class="config-panel-header">
          <div class="config-panel-heading-copy">
            <span class="config-panel-eyebrow">REMOTE LINK</span>
            <h2>{{ tr('远程连接', 'Remote Connection') }}</h2>
            <p>{{ tr('连接已部署的 MKLink 服务实例。', 'Connect to a deployed MKLink service instance.') }}</p>
          </div>
          <span :class="['badge', wsConnected ? 'badge-ok' : 'badge-err']">
            {{ wsConnected ? tr('已连接', 'Connected') : tr('未连接', 'Disconnected') }}
          </span>
        </header>
        <div class="form-row">
          <label class="form-label" for="remote-url">{{ tr('服务器地址', 'Server Address') }}</label>
          <input id="remote-url" v-model="remoteUrl" class="form-input" data-testid="remote-url" placeholder="ws://192.168.1.100:8765" />
        </div>
        <div class="form-row">
          <label class="form-label" for="remote-token">{{ tr('认证 Token', 'Authentication Token') }}</label>
          <input id="remote-token" v-model="remoteToken" class="form-input" data-testid="remote-token" type="password" :placeholder="tr('可选', 'Optional')" />
        </div>
        <div class="panel-actions">
          <button class="btn btn-primary" type="button" data-testid="connect-remote" :disabled="wsConnecting" @click="connectRemote">{{ tr('连接', 'Connect') }}</button>
          <button class="btn" type="button" data-testid="disconnect-remote" :disabled="!wsConnected" @click="wsDisconnect">{{ tr('断开', 'Disconnect') }}</button>
        </div>
      </section>

      <section v-else-if="activeSection === 'serve'" class="card serve-panel">
        <header class="config-panel-header">
          <div class="config-panel-heading-copy">
            <span class="config-panel-eyebrow">LOCAL SERVICE</span>
            <h2>{{ tr('启动服务', 'Start Service') }}</h2>
            <p>{{ tr('将当前工作站作为受控的远程调试端点。', 'Expose this workstation as a controlled remote debug endpoint.') }}</p>
          </div>
        </header>
        <div class="alert alert-info">{{ tr('在本地启动 MKLink 远程服务，供其他客户端连接。', 'Start the MKLink remote service locally for other clients.') }}</div>
        <div class="form-row">
          <label class="form-label" for="serve-host">{{ tr('绑定地址', 'Bind Address') }}</label>
          <input id="serve-host" v-model="serveConfig.host" class="form-input" data-testid="serve-host" />
        </div>
        <div class="form-row">
          <label class="form-label" for="serve-port">{{ tr('端口', 'Port') }}</label>
          <input id="serve-port" v-model.number="serveConfig.port" class="form-input" data-testid="serve-port" type="number" />
        </div>
        <div class="form-row">
          <label class="form-label" for="serve-token">Token</label>
          <input id="serve-token" v-model="serveConfig.token" class="form-input" data-testid="serve-token" type="password" :placeholder="tr('可选', 'Optional')" />
        </div>
        <div class="panel-actions">
          <button class="btn btn-primary" type="button" data-testid="launch-server" :disabled="launching" @click="launchServer">{{ tr('启动服务', 'Start Service') }}</button>
        </div>
      </section>

      <section v-else class="card firmware-panel" data-testid="firmware-upgrade-panel">
        <header class="config-panel-header">
          <div class="config-panel-heading-copy">
            <span class="config-panel-eyebrow">PROBE FIRMWARE</span>
            <h2>{{ tr('固件升级', 'Firmware Update') }}</h2>
            <p>{{ tr('读取 MICROKEEN U 盘版本，检查 GitHub/Gitee 最新固件并自动完成 UF2 升级。', 'Read the MICROKEEN drive version, check GitHub/Gitee, and complete the UF2 update automatically.') }}</p>
          </div>
          <span v-if="firmwareCheck?.current_version" class="firmware-version">{{ firmwareCheck.current_version }}</span>
        </header>
        <div class="firmware-upgrade-content">
          <button class="btn" type="button" data-testid="upgrade-firmware" :disabled="firmwareUpgrading || !deviceStatus.connected" @click="upgradeFirmware">
            {{ firmwareUpgrading ? tr('升级中...', 'Updating...') : tr('检查并升级固件', 'Check and Update Firmware') }}
          </button>
          <div v-if="manualFirmwareUpgrade" class="manual-firmware-download" data-testid="manual-firmware-download">
            <strong>{{ tr('自动升级未完成', 'Automatic update did not complete') }}</strong>
            <span>{{ tr('最新固件：', 'Latest firmware: ') }}{{ manualFirmwareUpgrade.latest_version }}</span>
            <p class="firmware-upgrade-status" data-testid="firmware-upgrade-status">{{ firmwareUpgradeStatus }}</p>
            <button class="btn icon-command" type="button" data-testid="download-firmware" :disabled="firmwareDownloading" @click="downloadFirmware">
              <Download :size="14" aria-hidden="true" />
              {{ firmwareDownloading ? tr('下载中...', 'Downloading...') : tr('下载固件', 'Download Firmware') }}
            </button>
            <span v-if="firmwareDownloadStatus" class="firmware-download-status" data-testid="firmware-download-status">{{ firmwareDownloadStatus }}</span>
          </div>
          <span v-else-if="firmwareUpgradeStatus" class="firmware-upgrade-status" data-testid="firmware-upgrade-status">{{ firmwareUpgradeStatus }}</span>
        </div>
      </section>
    </main>

    <div
      v-if="firmwareCheck?.status === 'upgrade_required'"
      class="firmware-banner"
      data-testid="firmware-warning"
    >
      <TriangleAlert :size="18" aria-hidden="true" />
      <span>{{ tr('探针固件需要升级', 'Probe firmware update required') }}</span>
      <button class="btn btn-sm" type="button" @click="showFirmwareModal = true">{{ tr('查看升级步骤', 'View Update Steps') }}</button>
      <button class="btn btn-sm" type="button" @click="recheckFirmware(true)">{{ tr('重新检测', 'Check Again') }}</button>
    </div>

    <FirmwareUpdateModal
      v-if="showFirmwareModal && firmwareCheck"
      :check="firmwareCheck"
      @close="showFirmwareModal = false"
      @recheck="recheckFirmware(true)"
    />
  </div>
</template>

<style scoped>
.config-workspace {
  display: grid;
  grid-template-columns: 184px minmax(0, 1fr);
  align-items: start;
  gap: 20px;
}

.section-content {
  min-width: 0;
}

.local-panel,
.remote-panel,
.serve-panel,
.firmware-panel {
  min-height: 252px;
}

.firmware-upgrade-content {
  display: grid;
  gap: 8px;
}

.firmware-version,
.firmware-upgrade-content p,
.firmware-upgrade-status {
  color: var(--muted);
  font-size: 12px;
}

.firmware-upgrade-content p {
  margin: 0;
  line-height: 1.5;
}

.firmware-upgrade-status {
  overflow-wrap: anywhere;
}

.manual-firmware-download {
  display: grid;
  justify-items: start;
  gap: 8px;
  margin-top: 4px;
  padding: 12px;
  border-left: 3px solid #f59e0b;
  background: #fffbeb;
  color: #7c4a03;
}

.manual-firmware-download p {
  margin: 0;
}

.firmware-download-status {
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.config-panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 24px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--line);
}

.config-panel-heading-copy {
  display: grid;
  gap: 3px;
}

.config-panel-eyebrow {
  color: var(--brand-text);
  font: 700 9px/1 var(--font-mono);
  letter-spacing: .13em;
}

.config-panel-header h2 {
  font-size: 16px;
  font-weight: 700;
}

.config-panel-heading-copy p {
  margin-top: 2px;
  color: var(--muted);
  font-size: 11px;
}

.icon-button,
.icon-command {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.icon-button {
  width: var(--control-height);
  padding: 0;
}

.icon-command {
  gap: 7px;
}

.local-actions,
.panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 18px 0 20px 110px;
}

.auto-save-state {
  color: var(--dim);
  font-size: 12px;
}

.firmware-banner {
  grid-column: 2;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: -8px;
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--warn) 58%, var(--line));
  border-radius: var(--radius-card);
  background: var(--warn-bg);
  color: var(--warn);
}

.firmware-banner span {
  margin-right: auto;
}

@media (max-width: 760px) {
  .config-workspace {
    grid-template-columns: 1fr;
  }

  .form-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
  }

  .form-label {
    grid-column: 1 / -1;
    width: auto;
    text-align: left;
  }

  .form-input {
    grid-column: 1 / -1;
    width: 100%;
    min-width: 0;
  }

  .form-select {
    width: 100%;
    min-width: 0;
  }

  .local-actions,
  .panel-actions {
    margin-left: 0;
    flex-wrap: wrap;
  }

  .firmware-banner {
    grid-column: 1;
    flex-wrap: wrap;
  }
}

@media (max-width: 460px) {
  .config-panel-header {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
