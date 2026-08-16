<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { RefreshCw, Search, TriangleAlert, Unplug, Usb } from '@lucide/vue'
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
import type { AxlStatus, FileSourceKind, PortInfo, ProbeFirmwareCheck, ProjectConfig } from '../types/mklink'
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
  setPowerOn,
  rebootProbe,
  parseAxf,
  probeFirmwareCheck,
} = useMklinkApi()
const { wsConnected, connect: wsConnect, disconnect: wsDisconnect } = useMklinkWs()
const toast = useToast()
const symbolCatalog = useSymbolCatalog()

const activeSection = ref<ConfigSection>('local')
const powerVoltages = [1800, 3300, 5000] as const
const config = ref<ProjectConfig>({})
const localPort = ref('')
const portOptions = ref<{ label: string; value: string }[]>([])
const localPortExplicit = ref(false)
const settings = ref<DesktopSettings>(loadDesktopSettings(window.localStorage))

const portsLoading = ref(false)
const savingLocal = ref(false)
const connecting = ref(false)
const disconnecting = ref(false)
const poweringVoltage = ref<1800 | 3300 | 5000 | null>(null)
const rebootingProbe = ref(false)
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

async function applyProbePower(voltageMv: 1800 | 3300 | 5000) {
  if (!deviceStatus.value.connected || poweringVoltage.value !== null) return
  const confirm5v = voltageMv === 5000
  if (confirm5v && !window.confirm(tr(
    '危险：5V 可能烧毁 3.3V 目标板。仅当你已核对原理图、供电路径和当前连接负载均可承受 5V 时才能继续。确认输出 5V？',
    'Danger: 5 V can destroy a 3.3 V target. Continue only after verifying the schematic, power path, and connected load are all 5 V tolerant. Output 5 V?',
  ))) return

  poweringVoltage.value = voltageMv
  try {
    await setPowerOn(voltageMv, confirm5v)
    toast.success(tr(`VCC 已设置为 ${(voltageMv / 1000).toFixed(1)}V`, `VCC set to ${(voltageMv / 1000).toFixed(1)} V`))
  } catch (error: any) {
    toast.error(tr('设置 VCC 失败: ', 'Failed to set VCC: ') + error.message)
  } finally {
    poweringVoltage.value = null
  }
}

async function restartProbe() {
  if (!deviceStatus.value.connected || rebootingProbe.value) return
  if (!window.confirm(tr(
    '重启 MKLink 探针会中断当前调试和数据流，并释放串口连接。确认继续？',
    'Rebooting the MKLink probe interrupts debugging and data streams and releases the serial connection. Continue?',
  ))) return

  rebootingProbe.value = true
  try {
    await rebootProbe()
    toast.success(tr('MKLink 已重启，请等待探针重新枚举后再连接', 'MKLink rebooted. Wait for the probe to enumerate before reconnecting.'))
  } catch (error: any) {
    toast.error(tr('MKLink 重启失败: ', 'Failed to reboot MKLink: ') + error.message)
  } finally {
    rebootingProbe.value = false
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

        <section class="probe-controls" data-testid="probe-controls" aria-labelledby="probe-controls-title">
          <div class="probe-controls-heading">
            <h3 id="probe-controls-title">{{ tr('探针电源与重启', 'Probe Power and Reboot') }}</h3>
            <span>{{ tr('命令会立即作用于当前接线', 'Commands immediately affect the connected hardware') }}</span>
          </div>
          <div class="probe-power-warning" role="note">
            <TriangleAlert :size="16" aria-hidden="true" />
            <span>{{ tr('输出电压前请核对目标板额定电压；5V 接入 3.3V 系统可能造成永久损坏。', 'Verify the target voltage rating before enabling VCC. Applying 5 V to a 3.3 V system can cause permanent damage.') }}</span>
          </div>
          <div class="probe-control-actions">
            <span class="probe-control-label">VCC</span>
            <button
              v-for="voltage in powerVoltages"
              :key="voltage"
              class="btn btn-sm"
              :class="{ 'danger-voltage': voltage === 5000 }"
              type="button"
              :data-testid="`probe-power-${voltage}`"
              :disabled="!deviceStatus.connected || poweringVoltage !== null || rebootingProbe"
              @click="applyProbePower(voltage)"
            >
              {{ poweringVoltage === voltage ? tr('设置中...', 'Applying...') : `${(voltage / 1000).toFixed(1)}V` }}
            </button>
            <button
              class="btn btn-sm icon-command probe-reboot"
              type="button"
              data-testid="reboot-probe"
              :disabled="!deviceStatus.connected || poweringVoltage !== null || rebootingProbe"
              @click="restartProbe"
            >
              <RefreshCw :size="14" aria-hidden="true" />
              {{ rebootingProbe ? tr('重启中...', 'Rebooting...') : tr('重启 MKLink', 'Reboot MKLink') }}
            </button>
          </div>
        </section>

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

      <section v-else class="card serve-panel">
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
.serve-panel {
  min-height: 252px;
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

.probe-controls {
  display: grid;
  gap: 10px;
  margin-top: 4px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface-soft, var(--surface));
}

.probe-controls-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.probe-controls-heading h3 {
  margin: 0;
  font-size: 14px;
}

.probe-controls-heading span,
.probe-control-label {
  color: var(--dim);
  font-size: 12px;
}

.probe-power-warning {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 8px 10px;
  border: 1px solid #f59e0b;
  border-radius: 4px;
  background: #fef3c7;
  color: #7c4a03;
  font-size: 12px;
  line-height: 1.45;
}

.probe-power-warning svg {
  flex: 0 0 auto;
  margin-top: 1px;
}

.probe-control-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.danger-voltage {
  border-color: var(--danger);
  color: var(--danger);
}

.probe-reboot {
  margin-left: auto;
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
  .panel-actions,
  .probe-controls-heading {
    margin-left: 0;
    flex-wrap: wrap;
  }

  .probe-reboot {
    margin-left: 0;
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
