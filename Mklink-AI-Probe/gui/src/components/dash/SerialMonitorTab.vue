<template>
  <div class="serial-assistant">
    <div class="serial-config-row">
      <label>
        <span>{{ tr('端口', 'Port') }}</span>
        <select v-model="portName" :disabled="running || starting || stopping || ymodemActive || ymodemStarting">
          <option v-for="port in ports" :key="port.device" :value="port.device">
            {{ port.device }}{{ port.description ? ` · ${port.description}` : '' }}
          </option>
        </select>
      </label>
      <button
        type="button" class="icon-action" :title="tr('刷新串口', 'Refresh ports')"
        :aria-label="tr('刷新串口', 'Refresh ports')" :disabled="running || refreshingPorts || ymodemActive || ymodemStarting"
        @click="refreshPorts"
      ><RefreshCw :size="15" :class="{ spinning: refreshingPorts }" /></button>
      <label>
        <span>{{ tr('波特率（可输入）', 'Baud Rate (editable)') }}</span>
        <input
          v-model="baudrate" data-testid="serial-baudrate" type="number"
          list="serial-baudrates" min="1" step="1" inputmode="numeric"
          :aria-invalid="validBaudrate === null"
          :disabled="running || starting || stopping || ymodemActive || ymodemStarting"
        />
        <datalist id="serial-baudrates">
          <option v-for="rate in baudrates" :key="rate" :value="rate" />
        </datalist>
      </label>
      <label>
        <span>{{ tr('数据位', 'Data Bits') }}</span>
        <select v-model="databits" :disabled="running || starting || stopping || ymodemActive || ymodemStarting">
          <option :value="8">8</option><option :value="7">7</option>
        </select>
      </label>
      <label>
        <span>{{ tr('停止位', 'Stop Bits') }}</span>
        <select v-model="stopbits" :disabled="running || starting || stopping || ymodemActive || ymodemStarting">
          <option :value="1">1</option><option :value="2">2</option>
        </select>
      </label>
      <label>
        <span>{{ tr('校验', 'Parity') }}</span>
        <select v-model="parity" :disabled="running || starting || stopping || ymodemActive || ymodemStarting">
          <option value="N">{{ tr('无', 'None') }}</option>
          <option value="E">{{ tr('偶', 'Even') }}</option>
          <option value="O">{{ tr('奇', 'Odd') }}</option>
        </select>
      </label>
      <button
        v-if="!running" type="button" class="btn btn-primary"
        :disabled="starting || !portName || validBaudrate === null"
        @click="doStart"
      >{{ starting ? tr('启动中', 'Starting') : tr('打开串口', 'Open Port') }}</button>
      <button v-else type="button" class="btn btn-danger" :disabled="stopping || ymodemActive || ymodemStarting" @click="doStop">
        {{ stopping ? tr('停止中', 'Stopping') : tr('关闭串口', 'Close Port') }}
      </button>
    </div>
    <SetupHint
      v-if="portsLoaded && !ports.length && !running"
      kind="info"
      :message="tr('未检测到可用串口。串口助手可独立于 MKLink 设备使用。', 'No serial ports detected. Serial Assistant works independently of the MKLink device.')"
      :primary-label="tr('刷新串口', 'Refresh Ports')"
      :busy="refreshingPorts"
      @primary="refreshPorts"
    />

    <div class="serial-toolbar">
      <div class="view-mode-switch" role="group" :aria-label="tr('显示模式', 'Display mode')">
        <button
          data-testid="serial-log-mode" type="button" :class="{ active: viewMode === 'log' }"
          :aria-pressed="viewMode === 'log'" :disabled="ymodemActive" @click="setViewMode('log')"
        ><ScrollText :size="14" /><span>{{ tr('日志', 'Log') }}</span></button>
        <button
          data-testid="serial-terminal-mode" type="button" :class="{ active: viewMode === 'terminal' }"
          :aria-pressed="viewMode === 'terminal'" @click="setViewMode('terminal')"
        ><SquareTerminal :size="14" /><span>{{ tr('终端', 'Terminal') }}</span></button>
      </div>
      <div class="serial-metrics">
        <span>RX {{ stats.rx_count }} / {{ stats.rx_bytes }} B</span>
        <span>TX {{ stats.tx_count }} / {{ stats.tx_bytes }} B</span>
        <span>{{ stats.bytes_per_sec }} B/s</span>
        <span>buffer {{ activeTelemetry?.bufferedSamples ?? 0 }}</span>
        <span>drops {{ activeTelemetry?.transportDroppedBatches ?? 0 }}/{{ activeTelemetry?.backendDroppedBatches ?? 0 }}</span>
        <span :class="{ 'status-error': currentPortStatus.startsWith('error:') }">
          {{ tr('端口', 'Port') }} {{ portName || '--' }} · {{ localizedPortStatus }}
        </span>
      </div>
      <span v-if="runtimeError" class="runtime-error" role="alert">{{ runtimeError }}</span>
      <button
        v-if="viewMode === 'log'" data-testid="serial-save-log" type="button"
        class="icon-action" :disabled="retainedCount === 0"
        :title="tr('保存日志', 'Save log')" :aria-label="tr('保存日志', 'Save log')"
        @click="saveLog"
      ><Download :size="15" /></button>
      <button
        type="button" class="icon-action clear-action"
        :title="viewMode === 'terminal' ? tr('清除终端', 'Clear terminal') : tr('清除日志', 'Clear log')"
        :aria-label="viewMode === 'terminal' ? tr('清除终端', 'Clear terminal') : tr('清除日志', 'Clear log')"
        @click="clearVisibleOutput"
      ><Trash2 :size="15" /></button>
    </div>

    <div class="ymodem-bar" data-testid="serial-ymodem">
      <strong>YMODEM</strong>
      <label class="ymodem-file-button" :class="{ disabled: ymodemActive || ymodemStarting }">
        {{ tr('选择文件', 'Choose file') }}
        <input
          data-testid="serial-ymodem-file" type="file"
          :disabled="ymodemActive || ymodemStarting" @change="chooseYmodemFile"
        >
      </label>
      <button
        v-if="!ymodemActive" data-testid="serial-ymodem-start" type="button" class="btn btn-primary ymodem-start-button"
        :disabled="ymodemStarting || !ymodemFile || !transmitEnabled" @click="startYmodem"
      >{{ ymodemStarting ? tr('准备中…', 'Preparing…') : tr('发送文件', 'Send file') }}</button>
      <button
        v-else data-testid="serial-ymodem-cancel" type="button" class="btn btn-danger"
        :disabled="ymodemCancelling" @click="cancelYmodem"
      >{{ ymodemCancelling ? tr('取消中…', 'Cancelling…') : tr('取消', 'Cancel') }}</button>
      <span class="ymodem-file-name" :title="ymodemFile?.name || ''">
        {{ ymodemFile ? `${ymodemFile.name} · ${ymodemFile.size} B` : tr('未选择文件', 'No file selected') }}
      </span>
      <progress v-if="ymodemStatus.transfer_id" :value="ymodemStatus.percent" max="100" />
      <span v-if="ymodemStatus.transfer_id" class="ymodem-progress">
        {{ ymodemStatus.percent }}% · {{ ymodemStatus.sent_bytes }}/{{ ymodemStatus.total_bytes }} B
      </span>
    </div>

    <VirtualLogPanel v-if="viewMode === 'log'" ref="logPanel" class="serial-log" />
    <div v-else class="serial-terminal-shell">
      <RttTerminalPanel
        ref="terminalPanel" :input-enabled="transmitEnabled"
        :aria-label="tr('串口终端', 'Serial terminal')" @input="queueTerminalInput"
      />
    </div>

    <RttTransmitBar
      id-prefix="serial" :enabled="transmitEnabled" :settings="transmitSettings"
      :send="sendSerial" @settings-change="persistTransmitSettings"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Download, RefreshCw, ScrollText, SquareTerminal, Trash2 } from '@lucide/vue'
import { useBinaryStream } from '../../composables/useBinaryStream'
import { useMklinkApi } from '../../composables/useMklinkApi'
import { useToast } from '../../composables/useToast'
import { tr } from '../../composables/useLanguage'
import type { DesktopSettings } from '../../lib/desktopSettings'
import { toHexPayload } from '../../lib/rttTransmit'
import { downloadTextFile, timestampedLogName } from '../../lib/downloadTextFile'
import {
  loadSerialAssistantSettings,
  saveSerialAssistantSettings,
  type SerialAssistantSettings,
} from '../../lib/serialAssistantSettings'
import type { PortInfo } from '../../types/mklink'
import RttTerminalPanel from './RttTerminalPanel.vue'
import RttTransmitBar from './RttTransmitBar.vue'
import SetupHint from './SetupHint.vue'
import VirtualLogPanel, { type VirtualLogInput } from './VirtualLogPanel.vue'
import { API_BASE } from '../../lib/runtimeEndpoint'

interface SerialStatus {
  running?: boolean
  ports?: Record<string, string>
  config?: Array<Record<string, unknown>>
  stats?: typeof stats.value
  ymodem?: YmodemStatus
}

interface YmodemStatus {
  transfer_id: number
  state: 'idle' | 'running' | 'completed' | 'failed' | 'cancelled'
  active: boolean
  phase: string
  port: string
  filename: string
  sent_bytes: number
  total_bytes: number
  percent: number
  block: number
  retries: number
  error: string
}

interface YmodemTraceEntry {
  seq: number
  transfer_id: number
  timestamp: number
  port: string
  direction: 'TX' | 'RX'
  size: number
  hex: string
}

interface YmodemTracePage {
  transfer_id: number
  entries: YmodemTraceEntry[]
  next_seq: number
  dropped: number
}

const idleYmodemStatus = (): YmodemStatus => ({
  transfer_id: 0, state: 'idle', active: false, phase: 'idle', port: '', filename: '',
  sent_bytes: 0, total_bytes: 0, percent: 0, block: 0, retries: 0, error: '',
})

const baudrates = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
const toast = useToast()
const { listPorts: fetchPorts } = useMklinkApi()
const logBinary = useBinaryStream('serial', {
  capacity: 5000, channelCount: 1, decoderMode: 'serial-log',
})
const terminalBinary = useBinaryStream('serial', {
  capacity: 64 * 1024, channelCount: 1, decoderMode: 'serial-terminal',
})
const ports = ref<PortInfo[]>([])
const portName = ref('')
const baudrate = ref(115200)
const databits = ref(8)
const stopbits = ref(1)
const parity = ref('N')
const running = ref(false)
const starting = ref(false)
const stopping = ref(false)
const refreshingPorts = ref(false)
const portsLoaded = ref(false)
const stats = ref({ rx_count: 0, tx_count: 0, rx_bytes: 0, tx_bytes: 0, bytes_per_sec: 0 })
const portStatuses = ref<Record<string, string>>({})
const runtimeError = ref('')
const viewMode = ref<'log' | 'terminal'>('terminal')
const logPanel = ref<InstanceType<typeof VirtualLogPanel> | null>(null)
const terminalPanel = ref<InstanceType<typeof RttTerminalPanel> | null>(null)
const serialSettings = ref<SerialAssistantSettings>(loadSerialAssistantSettings(localStorage))
const ymodemFile = ref<File | null>(null)
const ymodemStatus = ref<YmodemStatus>(idleYmodemStatus())
const ymodemStarting = ref(false)
const ymodemCancelling = ref(false)
const terminalEncoder = new TextEncoder()
let statusTimer: ReturnType<typeof setTimeout> | null = null
let ymodemTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false
let terminalInput = ''
let terminalInputTimer: ReturnType<typeof setTimeout> | null = null
let terminalSendChain = Promise.resolve()
let reportedPortError = ''
let attachedMode: 'log' | 'terminal' | null = null
let lastYmodemTerminalKey = ''
let reportedYmodemFinal = 0
let ymodemTraceCursor = 0
let ymodemTraceTransferId = 0

const currentPortStatus = computed(() => portStatuses.value[portName.value] || (running.value ? 'opening' : 'closed'))
const validBaudrate = computed(() => {
  const value = String(baudrate.value).trim()
  if (!/^\d+$/.test(value)) return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
})
const localizedPortStatus = computed(() => {
  const status = currentPortStatus.value
  if (status === 'open') return tr('已打开', 'Open')
  if (status === 'opening' || status === 'closed' && running.value) return tr('打开中', 'Opening')
  if (status === 'closed') return tr('已关闭', 'Closed')
  if (status === 'error: port is busy or unavailable') return tr('端口被占用或不可用', 'Port is busy or unavailable')
  return status
})
const ymodemActive = computed(() => ymodemStatus.value.active)
const transmitEnabled = computed(() => (
  running.value && currentPortStatus.value === 'open'
  && !ymodemActive.value && !ymodemStarting.value
))
const activeTelemetry = computed(() => (
  viewMode.value === 'log' ? logBinary.telemetry.value : terminalBinary.telemetry.value
))
const retainedCount = computed(() => logPanel.value?.retainedCount ?? 0)
const transmitSettings = computed<DesktopSettings>(() => ({
  version: 1,
  symbolPath: '',
  rttAddress: '',
  rttEncoding: 'utf-8',
  transmitMode: serialSettings.value.transmitMode,
  lineEnding: serialSettings.value.lineEnding,
  sendHistory: serialSettings.value.sendHistory.map(entry => ({ ...entry })),
}))

function errorMessage(payload: unknown, fallback: string): string {
  if (typeof payload === 'string' && payload) return payload
  if (typeof payload === 'object' && payload !== null) {
    const detail = (payload as Record<string, unknown>).detail
    if (typeof detail === 'string') return detail
    if (typeof detail === 'object' && detail !== null) {
      const conflict = (detail as Record<string, unknown>).conflict
      if (typeof conflict === 'string') return `${fallback}: ${conflict}`
    }
  }
  return fallback
}

async function requestJson(path: string, init?: RequestInit): Promise<any> {
  const response = await fetch(`${API_BASE}${path}`, init)
  let payload: unknown = null
  try { payload = await response.json() } catch { /* empty response */ }
  if (!response.ok) throw new Error(errorMessage(payload, `${response.status} ${response.statusText}`))
  return payload
}

async function refreshPorts(): Promise<void> {
  if (refreshingPorts.value) return
  refreshingPorts.value = true
  try {
    ports.value = await fetchPorts()
    if (!portName.value || !ports.value.some(port => port.device === portName.value)) {
      portName.value = ports.value[0]?.device || ''
    }
  } catch (caught) {
    toast.error(caught instanceof Error ? caught.message : String(caught))
  } finally {
    refreshingPorts.value = false
    portsLoaded.value = true
  }
}

function applyStatus(status: SerialStatus): void {
  running.value = status.running === true
  if (status.stats) stats.value = status.stats
  portStatuses.value = status.ports || {}
  if (status.ymodem) {
    applyYmodemStatus(status.ymodem)
    if (status.ymodem.active && ymodemTimer === null && !disposed) {
      ymodemTimer = setTimeout(pollYmodemStatus, 0)
    }
  }
  const config = status.config?.[0]
  if (running.value && config) {
    if (typeof config.port === 'string') portName.value = config.port
    if (typeof config.baudrate === 'number') baudrate.value = config.baudrate
    if (typeof config.databits === 'number') databits.value = config.databits
    if (typeof config.stopbits === 'number') stopbits.value = config.stopbits
    if (typeof config.parity === 'string') parity.value = config.parity
  }
  const portError = Object.values(portStatuses.value).find(value => value.startsWith('error:')) || ''
  if (portError && portError !== reportedPortError) {
    reportedPortError = portError
    toast.error(tr('串口打开失败：', 'Failed to open serial port: ') + (
      portError === 'error: port is busy or unavailable'
        ? tr('端口被占用或不可用', 'port is busy or unavailable')
        : portError.replace(/^error:\s*/, '')
    ))
  } else if (!portError) {
    reportedPortError = ''
  }
  syncBinaryStream()
}

function ymodemLine(status: YmodemStatus): string {
  if (status.phase === 'waiting') return tr('[YMODEM] 等待接收端发送 C…', '[YMODEM] Waiting for receiver C…')
  if (status.phase === 'transferring') return tr(
    `[YMODEM] 正在传输 ${status.percent}%（${status.sent_bytes}/${status.total_bytes} B）`,
    `[YMODEM] Transferring ${status.percent}% (${status.sent_bytes}/${status.total_bytes} B)`,
  )
  if (status.phase === 'retrying') return tr(
    `[YMODEM] 第 ${status.block} 块重试 ${status.retries}`,
    `[YMODEM] Retrying block ${status.block}, attempt ${status.retries}`,
  )
  if (status.phase === 'finishing') return tr('[YMODEM] 正在完成协议收尾…', '[YMODEM] Finishing transfer…')
  if (status.phase === 'cancelling') return tr('[YMODEM] 正在取消…', '[YMODEM] Cancelling…')
  if (status.state === 'completed') return tr('[YMODEM] 传输完成，等待设备重启。', '[YMODEM] Transfer complete; waiting for device reboot.')
  if (status.state === 'cancelled') return tr(`[YMODEM] 已取消：${status.error}`, `[YMODEM] Cancelled: ${status.error}`)
  if (status.state === 'failed') return tr(`[YMODEM] 失败：${status.error}`, `[YMODEM] Failed: ${status.error}`)
  return ''
}

function ymodemTerminalKey(status: YmodemStatus): string {
  if (!status.transfer_id) return ''
  if (status.phase === 'transferring') {
    return `${status.transfer_id}:transferring:${Math.floor(status.percent / 10)}`
  }
  if (status.phase === 'retrying') {
    return `${status.transfer_id}:retrying:${status.block}:${status.retries}`
  }
  return `${status.transfer_id}:${status.state}:${status.phase}`
}

function applyYmodemStatus(status: YmodemStatus): void {
  if (status.transfer_id && status.transfer_id !== ymodemTraceTransferId) {
    ymodemTraceTransferId = status.transfer_id
    ymodemTraceCursor = 0
  }
  ymodemStatus.value = { ...status }
  const line = ymodemLine(status)
  const terminalKey = line ? ymodemTerminalKey(status) : ''
  if (terminalKey && terminalKey !== lastYmodemTerminalKey) {
    lastYmodemTerminalKey = terminalKey
    terminalPanel.value?.write(`\r\n${line}\r\n`)
  }
  if (!status.active && status.transfer_id && reportedYmodemFinal !== status.transfer_id) {
    ymodemCancelling.value = false
    reportedYmodemFinal = status.transfer_id
    if (status.state === 'completed') toast.success(tr('YMODEM 文件传输完成', 'YMODEM file transfer completed'))
    else if (status.state === 'failed') toast.error(tr('YMODEM 传输失败：', 'YMODEM transfer failed: ') + status.error)
    else if (status.state === 'cancelled') toast.info(tr('YMODEM 传输已取消', 'YMODEM transfer cancelled'))
  }
}

function formatTraceTime(timestamp: number): string {
  const date = new Date(timestamp * 1000)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}.${String(date.getMilliseconds()).padStart(3, '0')}`
}

function ymodemTraceText(entry: YmodemTraceEntry): string {
  const bytes = entry.hex.trim().split(/\s+/).filter(Boolean)
  const color = entry.direction === 'TX' ? '\u001b[36m' : '\u001b[32m'
  const lines: string[] = []
  for (let offset = 0; offset < bytes.length; offset += 32) {
    const chunk = bytes.slice(offset, offset + 32).join(' ')
    const heading = `[${formatTraceTime(entry.timestamp)} YMODEM ${entry.direction} ${entry.size} B +0x${offset.toString(16).toUpperCase().padStart(4, '0')}]`
    lines.push(`${color}${heading}\u001b[0m ${chunk}`)
  }
  return `\r\n${lines.join('\r\n')}\r\n`
}

function applyYmodemTrace(page: YmodemTracePage): void {
  if (page.transfer_id && page.transfer_id !== ymodemTraceTransferId) {
    ymodemTraceTransferId = page.transfer_id
    ymodemTraceCursor = 0
  }
  if (page.dropped > 0) {
    terminalPanel.value?.write(tr(
      `\r\n[YMODEM TRACE] ${page.dropped} 条早期记录已被覆盖。\r\n`,
      `\r\n[YMODEM TRACE] ${page.dropped} earlier records were overwritten.\r\n`,
    ))
  }
  for (const entry of page.entries || []) {
    if (entry.seq <= ymodemTraceCursor) continue
    terminalPanel.value?.write(ymodemTraceText(entry))
    ymodemTraceCursor = entry.seq
  }
  ymodemTraceCursor = Math.max(ymodemTraceCursor, page.next_seq || 0)
}

function chooseYmodemFile(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] || null
  runtimeError.value = ''
  if (!file) {
    ymodemFile.value = null
    return
  }
  const filenameBytes = terminalEncoder.encode(file.name).length
  if (!file.size) {
    runtimeError.value = tr('YMODEM 文件不能为空', 'YMODEM file cannot be empty')
  } else if (file.size > 32 * 1024 * 1024) {
    runtimeError.value = tr('YMODEM 文件不能超过 32 MiB', 'YMODEM file cannot exceed 32 MiB')
  } else if (filenameBytes > 31) {
    runtimeError.value = tr('YMODEM 文件名不能超过 31 个 UTF-8 字节', 'YMODEM filename cannot exceed 31 UTF-8 bytes')
  }
  if (runtimeError.value) {
    ymodemFile.value = null
    input.value = ''
    toast.error(runtimeError.value)
    return
  }
  ymodemFile.value = file
}

async function pollYmodemStatus(): Promise<void> {
  if (ymodemTimer !== null) {
    clearTimeout(ymodemTimer)
    ymodemTimer = null
  }
  let tracePending = false
  try {
    applyYmodemStatus(await requestJson('/api/dash/serial/ymodem/status'))
    const tracePage = await requestJson(
      `/api/dash/serial/ymodem/trace?after=${ymodemTraceCursor}&limit=128`,
    ) as YmodemTracePage
    applyYmodemTrace(tracePage)
    tracePending = tracePage.entries.length >= 128
  } catch (caught) {
    runtimeError.value = caught instanceof Error ? caught.message : String(caught)
  }
  if (!disposed && (ymodemStatus.value.active || ymodemCancelling.value || tracePending)) {
    ymodemTimer = setTimeout(pollYmodemStatus, 150)
  }
}

async function startYmodem(): Promise<void> {
  const file = ymodemFile.value
  if (!file || !transmitEnabled.value || ymodemStarting.value) return
  ymodemStarting.value = true
  runtimeError.value = ''
  ymodemTraceCursor = 0
  ymodemTraceTransferId = 0
  if (viewMode.value !== 'terminal') setViewMode('terminal')
  await nextTick()
  terminalPanel.value?.activate()
  terminalPanel.value?.write(tr(
    `\r\n[YMODEM] 准备发送 ${file.name}（${file.size} B）…\r\n`,
    `\r\n[YMODEM] Preparing ${file.name} (${file.size} B)…\r\n`,
  ))
  const form = new FormData()
  form.append('file', file, file.name)
  try {
    const status = await requestJson(
      `/api/dash/serial/ymodem/start?port=${encodeURIComponent(portName.value)}`,
      { method: 'POST', body: form },
    )
    applyYmodemStatus(status)
    void pollYmodemStatus()
  } catch (caught) {
    runtimeError.value = caught instanceof Error ? caught.message : String(caught)
    terminalPanel.value?.write(tr(
      `\r\n[YMODEM] 启动失败：${runtimeError.value}\r\n`,
      `\r\n[YMODEM] Failed to start: ${runtimeError.value}\r\n`,
    ))
    toast.error(tr('YMODEM 启动失败：', 'Failed to start YMODEM: ') + runtimeError.value)
  } finally {
    ymodemStarting.value = false
  }
}

async function cancelYmodem(): Promise<void> {
  if (!ymodemActive.value || ymodemCancelling.value) return
  ymodemCancelling.value = true
  try {
    applyYmodemStatus(await requestJson('/api/dash/serial/ymodem/cancel', { method: 'POST' }))
    void pollYmodemStatus()
  } catch (caught) {
    ymodemCancelling.value = false
    runtimeError.value = caught instanceof Error ? caught.message : String(caught)
    toast.error(tr('取消 YMODEM 失败：', 'Failed to cancel YMODEM: ') + runtimeError.value)
  }
}

async function refreshStatus(): Promise<void> {
  try {
    applyStatus(await requestJson('/api/dash/serial/status'))
  } catch { /* retry on the next low-rate poll */ }
}

async function pollStatus(): Promise<void> {
  await refreshStatus()
  if (!disposed) statusTimer = setTimeout(pollStatus, 1000)
}

async function doStart(): Promise<void> {
  if (!portName.value || validBaudrate.value === null || starting.value) return
  starting.value = true
  runtimeError.value = ''
  try {
    const result = await requestJson('/api/dash/serial/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ports: [{
          port: portName.value,
          baudrate: validBaudrate.value,
          databits: databits.value,
          stopbits: stopbits.value,
          parity: parity.value,
        }],
      }),
    })
    if (result?.status !== 'already_running') {
      logBinary.reset()
      terminalBinary.reset()
      logPanel.value?.clear()
      terminalPanel.value?.clear()
      stats.value = { rx_count: 0, tx_count: 0, rx_bytes: 0, tx_bytes: 0, bytes_per_sec: 0 }
    }
    running.value = true
    portStatuses.value = { [portName.value]: 'opening' }
    syncBinaryStream()
    await refreshStatus()
    toast.success(tr('串口助手已启动', 'Serial Assistant started'))
  } catch (caught) {
    runtimeError.value = caught instanceof Error ? caught.message : String(caught)
    toast.error(tr('启动失败：', 'Start failed: ') + runtimeError.value)
  } finally {
    starting.value = false
  }
}

async function doStop(): Promise<void> {
  if (!running.value || stopping.value) return
  stopping.value = true
  runtimeError.value = ''
  try {
    await requestJson('/api/dash/serial/stop', { method: 'POST' })
    running.value = false
    portStatuses.value = {}
    detachBinaryStreams()
    toast.info(tr('串口助手已停止', 'Serial Assistant stopped'))
  } catch (caught) {
    runtimeError.value = caught instanceof Error ? caught.message : String(caught)
    toast.error(tr('停止失败：', 'Stop failed: ') + runtimeError.value)
  } finally {
    stopping.value = false
  }
}

function syncBinaryStream(): void {
  if (!running.value) {
    detachBinaryStreams()
    return
  }
  if (attachedMode === viewMode.value) return
  if (attachedMode === 'log') logBinary.stop()
  else if (attachedMode === 'terminal') terminalBinary.stop()
  if (viewMode.value === 'log') logBinary.start()
  else terminalBinary.start()
  attachedMode = viewMode.value
}

function detachBinaryStreams(): void {
  logBinary.stop()
  terminalBinary.stop()
  attachedMode = null
}

watch(() => terminalBinary.serialTerminal.value, chunk => {
  if (viewMode.value === 'terminal' && chunk?.text) terminalPanel.value?.write(chunk.text)
})

watch(() => logBinary.serialLines.value, batch => {
  if (viewMode.value !== 'log' || !batch) return
  logPanel.value?.append(batch.lines.map(line => ({
    time: line.timestampNs,
    level: line.direction === 'RX' ? 'data' : 'warning',
    label: line.direction,
    text: `${line.rawHex}${line.ascii ? `  ${visibleAscii(line.ascii)}` : ''}`,
  } satisfies VirtualLogInput)))
})

watch([() => logBinary.error.value, () => terminalBinary.error.value], ([logError, terminalError]) => {
  const activeError = viewMode.value === 'log' ? logError : terminalError
  if (activeError) runtimeError.value = activeError
})

async function sendSerial(payload: Uint8Array): Promise<void> {
  if (!transmitEnabled.value) throw new Error(tr('串口尚未打开', 'Serial port is not open'))
  await requestJson('/api/dash/serial/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port: portName.value, data: toHexPayload(payload), hex: true }),
  })
}

function persistTransmitSettings(next: DesktopSettings): void {
  serialSettings.value = saveSerialAssistantSettings(localStorage, {
    transmitMode: next.transmitMode,
    lineEnding: next.lineEnding,
    sendHistory: next.sendHistory,
  })
}

function setViewMode(mode: 'log' | 'terminal'): void {
  if (viewMode.value === mode) return
  viewMode.value = mode
  syncBinaryStream()
  if (mode === 'terminal') void nextTick(() => terminalPanel.value?.activate())
}

function queueTerminalInput(data: string): void {
  if (!transmitEnabled.value || !data) return
  terminalInput += data
  if (terminalInputTimer === null) terminalInputTimer = setTimeout(flushTerminalInput, 8)
}

function flushTerminalInput(): void {
  terminalInputTimer = null
  if (!terminalInput) return
  const payload = terminalEncoder.encode(terminalInput)
  terminalInput = ''
  terminalSendChain = terminalSendChain
    .then(() => sendSerial(payload))
    .then(() => { runtimeError.value = '' })
    .catch(caught => { runtimeError.value = caught instanceof Error ? caught.message : String(caught) })
}

function clearVisibleOutput(): void {
  if (viewMode.value === 'terminal') terminalPanel.value?.clear()
  else logPanel.value?.clear()
}

function saveLog(): void {
  const text = logPanel.value?.exportText() || ''
  if (text) downloadTextFile(timestampedLogName('serial'), text)
}

function visibleAscii(value: string): string {
  return value.replace(/\r/g, '\\r').replace(/\n/g, '\\n')
}

onMounted(async () => {
  await refreshPorts()
  await refreshStatus()
  void pollStatus()
})

onUnmounted(() => {
  disposed = true
  detachBinaryStreams()
  if (statusTimer !== null) clearTimeout(statusTimer)
  if (ymodemTimer !== null) clearTimeout(ymodemTimer)
  if (terminalInputTimer !== null) clearTimeout(terminalInputTimer)
  terminalInput = ''
})
</script>

<style scoped>
.serial-assistant { display: flex; flex: 1 1 auto; min-width: 0; min-height: 0; flex-direction: column; }
.serial-config-row { display: flex; flex-wrap: wrap; align-items: end; gap: 7px; }
.serial-config-row label { display: grid; gap: 3px; color: var(--muted); font-size: 11px; }
.serial-config-row select, .serial-config-row input { height: 30px; min-width: 64px; max-width: 220px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: inherit; }
.serial-config-row input { width: 90px; }
.serial-config-row label:first-child select { width: 180px; }
.serial-toolbar { display: flex; min-width: 0; align-items: center; gap: 10px; margin-top: 8px; }
.ymodem-bar { display: grid; grid-template-columns:auto auto auto minmax(120px,1fr) auto auto; align-items:center; gap:7px; margin-top:8px; padding:7px 8px; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); font-size:11px; }
.ymodem-bar strong { color:var(--accent); letter-spacing:.04em; }
.ymodem-file-button { position:relative; overflow:hidden; padding:5px 8px; border:1px solid var(--border); border-radius:4px; cursor:pointer; white-space:nowrap; }
.ymodem-file-button input { position:absolute; inset:0; width:100%; height:100%; opacity:0; cursor:pointer; }
.ymodem-file-button.disabled { opacity:.45; cursor:not-allowed; }
.ymodem-file-name { min-width:0; overflow:hidden; color:var(--muted); text-overflow:ellipsis; white-space:nowrap; }
.ymodem-bar progress { width:90px; }
.ymodem-progress { color:var(--muted); font-family:var(--font-mono); white-space:nowrap; }
.view-mode-switch { display: inline-flex; flex: 0 0 auto; overflow: hidden; border: 1px solid var(--border); border-radius: 4px; }
.view-mode-switch button { display: inline-flex; height: 26px; align-items: center; gap: 5px; padding: 0 8px; border: 0; background: var(--surface); color: var(--muted); cursor: pointer; }
.view-mode-switch button + button { border-left: 1px solid var(--border); }
.view-mode-switch button.active { background: var(--accent); color: #fff; }
.serial-metrics { display: flex; min-width: 0; flex: 1 1 auto; flex-wrap: wrap; gap: 0; color: var(--muted); font-family: var(--font-mono); font-size: 11px; }
.serial-metrics span + span::before { margin: 0 6px; color: var(--dim); content: '\00b7'; }
.runtime-error, .status-error { color: var(--danger); }
.runtime-error { min-width: 0; overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.icon-action { display: inline-grid; width: 30px; height: 30px; place-items: center; padding: 0; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: var(--muted); cursor: pointer; }
.clear-action { width: 26px; height: 26px; margin-left: auto; }
.icon-action:hover { border-color: var(--accent); color: var(--accent); }
.spinning { animation: spin 0.8s linear infinite; }
.serial-log { flex: 1 1 auto; min-height: 260px; margin-top: 8px; border: 1px solid var(--border); border-radius: var(--radius); }
.serial-terminal-shell { display: flex; flex: 1 1 auto; min-height: 260px; overflow: hidden; }
.serial-terminal-shell :deep(.rtt-terminal-panel) { width: 100%; }
.serial-assistant :deep(.rtt-transmit-wrapper) { margin-top: 8px; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 768px) {
  .serial-config-row label:first-child { flex: 1 1 180px; }
  .serial-config-row label:first-child select { width: 100%; max-width: none; }
  .serial-toolbar { align-items: flex-start; flex-wrap: wrap; }
  .serial-metrics { order: 3; flex-basis: 100%; }
  .ymodem-bar { grid-template-columns:auto auto minmax(80px,1fr) auto; }
  .ymodem-bar progress,.ymodem-progress { grid-column:span 2; }
}
</style>
