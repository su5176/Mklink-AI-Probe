<template>
  <div class="sv-tab">
    <input
      ref="fileInput"
      class="sv-file-input"
      type="file"
      accept=".jsonl,application/x-ndjson,application/json"
      @change="onImportFileChange"
    >
    <SetupHint
      v-if="!deviceConnected && !offlineMode"
      kind="device"
      :message="tr('实时跟踪需要 MKLink 设备；已保存的 JSONL 可直接离线回放。', 'Live trace requires an MKLink device; saved JSONL can be replayed offline.')"
      :primary-label="tr('连接设备', 'Connect Device')"
      :secondary-label="tr('导入 JSONL', 'Import JSONL')"
      :busy="connecting"
      @primary="quickConnect"
      @secondary="triggerImport"
    />
    <template v-if="deviceConnected || offlineMode">
      <div v-if="deviceConnected && !offlineMode" class="sv-address-row">
        <label for="systemview-rtt-address">{{ tr('RTT 地址', 'RTT Address') }}</label>
        <input
          id="systemview-rtt-address" v-model="rttAddress" data-testid="systemview-rtt-address"
          type="text" spellcheck="false" placeholder="0x20000000" @input="onAddressInput"
        >
        <button
          data-testid="systemview-rtt-search" type="button" class="btn-search"
          :disabled="searching || starting" @click="searchRttAddress"
        >
          <Search :size="15" />
          <span>{{ searching ? tr('搜索中', 'Searching') : tr('自动搜索', 'Auto Search') }}</span>
        </button>
        <span v-if="addressError" class="address-error" role="alert">{{ addressError }}</span>
        <span v-else-if="addressSource" class="address-source">{{ tr('来源:', 'Source:') }} {{ addressSource }}</span>
      </div>
      <SetupHint
        v-if="deviceConnected && !offlineMode && !hasAddressFileSource"
        kind="symbols"
        :message="tr('加载 AXF / ELF 后，自动搜索可直接定位 RTOS Trace 的 RTT 控制块。', 'Load AXF / ELF so Auto Search can locate the RTOS Trace RTT control block.')"
        :primary-label="tr('加载 AXF / ELF', 'Load AXF / ELF')"
        :busy="loadingSymbols"
        @primary="loadSymbolFile"
      />
      <div class="sv-toolbar">
        <ControlToolbar
          v-if="!offlineMode"
          :state="toolbarState"
          :error="runtimeError || dash.error.value"
          :device-connected="deviceConnected && !searching"
          @start="onStart"
          @pause="onPauseRender"
          @resume="onResumeRender"
          @stop="onStop"
        />
        <button v-else class="btn-clear sv-mode-btn" @click="returnToLive">{{ tr('返回实时', 'Back to Live') }}</button>
        <button
          v-if="!offlineMode" data-testid="systemview-recording" type="button"
          class="btn-clear sv-tool-btn sv-record-btn" :class="{ active: meta.recording }"
          :disabled="recordingBusy || dash.state.value !== 'running'" @click="toggleRecording"
          :title="meta.recording ? tr('停止保存并完成当前 JSONL 文件', 'Stop and finalize the current JSONL file') : tr('将后续事件实时保存为 JSONL', 'Save subsequent events to JSONL in real time')"
        >
          <Square v-if="meta.recording" :size="13" />
          <Circle v-else :size="13" />
          <span>{{ meta.recording ? tr('停止保存', 'Stop Saving') : tr('实时保存', 'Record') }}</span>
        </button>
        <button class="btn-clear sv-tool-btn" @click="triggerImport">{{ tr('导入 JSONL', 'Import JSONL') }}</button>
        <button class="btn-clear sv-tool-btn" :disabled="!currentJsonlPath" @click="exportLog(currentJsonlPath)">{{ tr('导出 JSONL', 'Export JSONL') }}</button>
        <button class="btn-clear sv-tool-btn" :disabled="!currentSummaryPath" @click="exportLog(currentSummaryPath)">{{ tr('导出摘要', 'Export Summary') }}</button>
        <label class="sv-window">
          {{ tr('窗口', 'Window') }}
          <select v-model.number="windowUs">
            <option :value="500_000">0.5s</option>
            <option :value="1_000_000">1s</option>
            <option :value="2_000_000">2s</option>
            <option :value="5_000_000">5s</option>
            <option :value="10_000_000">10s</option>
            <option :value="30_000_000">30s</option>
            <option :value="60_000_000">60s</option>
          </select>
        </label>
      </div>
      <div v-if="offlineMode" class="sv-replay-bar" data-testid="systemview-replay-controls">
        <button
          type="button" class="btn-clear sv-replay-command"
          :disabled="replayState === 'ended'" @click="toggleReplay"
          :title="replayState === 'playing' ? tr('暂停回放', 'Pause replay') : tr('继续回放', 'Resume replay')"
        >
          <Pause v-if="replayState === 'playing'" :size="14" />
          <Play v-else :size="14" />
          <span>{{ replayState === 'playing' ? tr('暂停', 'Pause') : tr('播放', 'Play') }}</span>
        </button>
        <button type="button" class="btn-clear sv-replay-command" @click="restartReplay" :title="tr('从头重新回放', 'Replay from the beginning')">
          <RotateCcw :size="14" />
          <span>{{ tr('重新播放', 'Restart') }}</span>
        </button>
        <label class="sv-replay-speed">
          {{ tr('速度', 'Speed') }}
          <select v-model.number="replaySpeed">
            <option :value="0.5">0.5x</option>
            <option :value="1">1x</option>
            <option :value="2">2x</option>
            <option :value="4">4x</option>
            <option :value="10">10x</option>
          </select>
        </label>
        <progress :value="replayProgress" max="100"></progress>
        <span class="sv-replay-progress">{{ replayProgress.toFixed(1) }}%</span>
        <span class="sv-replay-status" :title="importStatus">{{ importStatus }}</span>
      </div>

      <div class="sv-health-grid">
        <div class="sv-health-card">
          <span>Events</span>
          <b>{{ eventCount.toLocaleString() }}</b>
        </div>
        <div class="sv-health-card">
          <span>Tasks</span>
          <b>{{ taskCount }}</b>
        </div>
        <div class="sv-health-card" :class="{ warn: meta.dropped > 0 }">
          <span>Runtime Drop</span>
          <b :title="meta.sessionDropped ? `session dropped: ${meta.sessionDropped}` : 'runtime dropped'">{{ meta.dropped.toLocaleString() }}</b>
        </div>
        <div v-if="meta.targetOverflowEvents > 0 || meta.targetDroppedPackets > 0" class="sv-health-card warn">
          <span>Target Overflow</span>
          <b :title="`target dropped packets: ${meta.targetDroppedPackets.toLocaleString()}`">{{ meta.targetOverflowEvents.toLocaleString() }}</b>
        </div>
        <div class="sv-health-card" :class="{ warn: !meta.cpuFreq }">
          <span>CPU Clock</span>
          <b :title="meta.cpuFreqSource || 'cpu_freq'">{{ meta.cpuFreq ? fmtCpuFreq(meta.cpuFreq) : 'Unknown' }}</b>
        </div>
        <div class="sv-health-card" :class="{ warn: !meta.synced && dash.state.value === 'running' }">
          <span>Sync</span>
          <b>{{ meta.synced || dash.state.value !== 'running' ? 'Ready' : 'Unsynced' }}</b>
        </div>
        <div v-if="analysisBufferCount" class="sv-health-card">
          <span>Analysis Buffer</span>
          <b>{{ analysisBufferCount.toLocaleString() }}</b>
        </div>
        <div v-if="offlineMode || importStatus || meta.recordingError" class="sv-health-card sv-health-wide" :class="{ warn: importError || !!meta.recordingError }">
          <span>{{ offlineMode ? 'Offline Log' : 'Status' }}</span>
          <b :title="offlineFileName || importStatus || meta.recordingError">{{ offlineFileName || importStatus || meta.recordingError }}</b>
        </div>
      </div>

      <div class="sv-section sv-events-section" :class="{ collapsed: !showEventStream }">
        <div class="sv-section-title">
          <span>Events List</span>
          <span class="sv-section-subtitle">{{ tr(`最近 ${eventRows.length} 条`, `Latest ${eventRows.length}`) }}</span>
          <span class="sv-section-actions">
            <button v-if="eventList.length > 0" class="btn-clear" @click="clearAll">{{ tr('清除', 'Clear') }}</button>
            <button class="btn-clear" @click="showEventStream = !showEventStream">
              {{ showEventStream ? tr('折叠', 'Collapse') : tr('展开', 'Expand') }}
            </button>
          </span>
        </div>
        <div v-if="showEventStream" class="sv-table-wrap sv-events-table-wrap">
          <table class="sv-table sv-events-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Time</th>
                <th>Context</th>
                <th>Event</th>
                <th>Resource</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in eventRows" :key="row.index" :class="evtColor(row.kind)">
                <td>{{ row.index }}</td>
                <td>{{ row.time }}</td>
                <td>{{ row.context }}</td>
                <td>{{ row.event }}</td>
                <td>{{ row.resource }}</td>
                <td>{{ row.detail }}</td>
              </tr>
              <tr v-if="eventRows.length === 0">
                <td colspan="6" class="sv-empty-cell">{{ tr('等待 SystemView 事件。', 'Waiting for SystemView events.') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 交互式 SystemView 时间轴 -->
      <div class="sv-section sv-gantt-section">
        <div class="sv-section-title">
          <span>Timeline</span>
          <span class="sv-section-subtitle">{{ tr('微秒标尺与任务运行区间', 'Microsecond scale and execution intervals') }}</span>
          <button class="btn-clear" @click="tlReset">{{ tr('全览', 'Fit All') }}</button>
        </div>
        <div class="sv-legend" ref="tlLegend"></div>
        <div class="sv-canvas-wrap"><canvas ref="tlCanvas" :title="tr('滚轮缩放，按住鼠标左键拖动，双击恢复全览', 'Wheel to zoom, drag with the left mouse button, double-click to fit all')"></canvas></div>
        <div class="sv-tip" ref="tlTip"></div>
      </div>

      <div class="sv-bottom-grid">
        <div class="sv-section sv-runtime-section">
          <div class="sv-section-title">
            <span>Runtime</span>
            <span class="sv-section-subtitle">{{ tr('单次运行片段分布', 'Single-run segment distribution') }}</span>
          </div>
          <div class="sv-table-wrap">
            <table class="sv-table sv-runtime-table">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Count</th>
                  <th>Min</th>
                  <th>25%</th>
                  <th>50%</th>
                  <th>75%</th>
                  <th>Max</th>
                  <th>CPU</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in runtimeRows" :key="row.id">
                  <td class="sv-name-cell"><i :style="{ background: row.color }"></i>{{ row.name || hexId(row.id) }}</td>
                  <td>{{ formatScheduleCount(row.count) }}</td>
                  <td>{{ fmtDurationUs(row.minUs) }}</td>
                  <td>{{ fmtDurationUs(row.p25Us) }}</td>
                  <td>{{ fmtDurationUs(row.p50Us) }}</td>
                  <td>{{ fmtDurationUs(row.p75Us) }}</td>
                  <td>{{ fmtDurationUs(row.maxUs) }}</td>
                  <td class="sv-meter-cell">
                    <div class="sv-inline-meter"><span :style="{ width: clamp(row.pct) + '%', background: row.color }"></span></div>
                    <em>{{ row.pct.toFixed(1) }}%</em>
                  </td>
                </tr>
                <tr v-if="runtimeRows.length === 0">
                  <td colspan="8" class="sv-empty-cell">{{ tr('还没有运行片段。', 'No runtime segments yet.') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="sv-section sv-context-section">
          <div class="sv-section-title">
            <span>Context</span>
            <span class="sv-section-subtitle">{{ tr('任务活动概览', 'Task activity overview') }}</span>
          </div>
          <div class="sv-table-wrap">
            <table class="sv-table sv-context-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Prio</th>
                  <th>Activations</th>
                  <th>Total Run</th>
                  <th>CPU Load</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in contextRows" :key="row.id">
                  <td class="sv-name-cell"><i :style="{ background: row.color }"></i>{{ row.name || hexId(row.id) }}</td>
                  <td>{{ row.type }}</td>
                  <td>{{ row.priority ?? '-' }}</td>
                  <td>{{ formatScheduleCount(row.activations) }}</td>
                  <td>{{ fmtDurationUs(row.totalRunUs) }}</td>
                  <td class="sv-meter-cell">
                    <div class="sv-inline-meter"><span :style="{ width: clamp(row.cpuLoad) + '%', background: row.color }"></span></div>
                    <em>{{ row.cpuLoad.toFixed(1) }}%</em>
                  </td>
                </tr>
                <tr v-if="contextRows.length === 0">
                  <td colspan="6" class="sv-empty-cell">{{ tr('还没有任务上下文。', 'No task contexts yet.') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, shallowRef, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { Circle, Pause, Play, RotateCcw, Search, Square } from '@lucide/vue'
import { useDashboard } from '../../composables/useDashboard'
import { useEventSource } from '../../composables/useEventSource'
import { useBinaryStream } from '../../composables/useBinaryStream'
import { useResourceStatus } from '../../composables/useResourceStatus'
import { useMklinkApi } from '../../composables/useMklinkApi'
import { useDashboardSetup } from '../../composables/useDashboardSetup'
import {
  DESKTOP_SETTINGS_CHANGED_EVENT,
  isMapFilePath,
  isSymbolFilePath,
  loadDesktopSettings,
  saveDesktopSettings,
  type DesktopSettings,
} from '../../lib/desktopSettings'
import { SvTimeline } from '../../lib/svTimeline'
import { AdaptiveFrameRateController, RenderScheduler } from '../../lib/stream/renderScheduler'
import { appendManyToLast } from '../../lib/boundedBuffer'
import { takeNewStreamPoints } from '../../lib/streamCursor'
import { ingestSystemViewIntervals, type SystemViewIntervalState } from '../../lib/systemViewIntervals'
import {
  buildSystemViewEventRows,
  computeContextRows,
  computeRuntimeRows,
  normalizeSystemViewName,
} from '../../lib/systemViewMetrics'
import { appendAndTrimEventsByTime, appendAndTrimRanges, filterRangesByWindow } from '../../lib/systemViewTimeBuffer'
import { formatScheduleCount } from '../../lib/systemViewLabels'
import { importSystemViewJsonl } from '../../lib/systemViewImport'
import ControlToolbar from './ControlToolbar.vue'
import SetupHint from './SetupHint.vue'
import { language, tr } from '../../composables/useLanguage'
import { API_BASE } from '../../lib/runtimeEndpoint'

const props = defineProps<{ deviceConnected: boolean }>()

const dash = useDashboard('systemview')
const { data: statusData, connect: connectStatus, disconnect: disconnectStatus } = useEventSource('/api/dash/systemview/stream', {
  passthroughEvents: ['status'],
})
const binaryStream = useBinaryStream('systemview', { capacity: 300_000, channelCount: 1 })
const renderPaused = ref(false)
const desktopStorage = localStorage
const settings = ref<DesktopSettings>(loadDesktopSettings(desktopStorage))
const hasAddressFileSource = computed(() => (
  isSymbolFilePath(settings.value.symbolPath) || isMapFilePath(settings.value.mapPath)
))
const rttAddress = ref(settings.value.rttAddress)
const addressError = ref('')
const addressSource = ref('')
const searching = ref(false)
const starting = ref(false)
const runtimeError = ref<string | null>(null)
const { findRtt } = useMklinkApi()
const { connecting, loadingSymbols, quickConnect, loadSymbolFile } = useDashboardSetup()
const toolbarState = computed(() => (
  runtimeError.value ? 'error' :
    starting.value ? 'starting' :
      dash.state.value === 'running' && renderPaused.value ? 'paused' : dash.state.value
))
const { checkConflict } = useResourceStatus()

// ---- 状态 ----
interface TaskStat {
  id: number
  rawId: number
  name: string
  color: string
  runUs: number
  switches: number
  prio?: number
  type?: string
  stackBase?: number
  stackSize?: number
}
interface TaskInterval { taskId: number; start: number; end: number; startTk?: number | bigint; endTk?: number | bigint }
interface SystemViewLogItem { path: string; summary_path?: string }

const PALETTE = ['#5b8cff', '#21c7a8', '#f5a623', '#e056fd', '#ff7675', '#fdcb6e',
                 '#00cec9', '#a29bfe', '#55efc4', '#fab1a0', '#74b9ff', '#fd79a8']
const fileInput = ref<HTMLInputElement | null>(null)
const eventList = shallowRef<any[]>([])
let analysisEvents: any[] = []
const analysisBufferCount = ref(0)
const taskStats = reactive<Record<number, TaskStat>>({})
const intervals = shallowRef<TaskInterval[]>([])
// The worker returns a pixel envelope for the requested viewport. Its bucket
// boundaries move as the follow window advances, so replacing the previous
// response makes whole groups of intervals disappear and reappear. Keep a
// bounded live cache keyed by interval start and update it incrementally;
// rendering still filters this cache to the current viewport.
const visibleIntervalCache = new Map<string, TaskInterval>()
const exactRuntimeRows = shallowRef<any[]>([])
let intervalState: SystemViewIntervalState = { currentTaskId: null, currentStart: null }
const idleUs = ref(0)
let firstT = 0
let lastT = 0
const meta = reactive({
  synced: false,
  dropped: 0,
  sessionDropped: 0,
  targetOverflowEvents: 0,
  targetDroppedPackets: 0,
  targetDropCount: null as number | null,
  cpuFreq: 0,
  cpuFreqSource: '',
  taskNames: {} as Record<number, string>,
  isrNames: {} as Record<number, string>,
  recording: false,
  recordingPath: '',
  recordingSummaryPath: '',
  recordingError: '',
})
let lastStreamSeq = 0
const totalEventCount = ref(0)
const windowUs = ref(10_000_000)
const showEventStream = ref(true)
const offlineMode = ref(false)
const offlineFileName = ref('')
const importStatus = ref('')
const importError = ref(false)
const latestLog = ref<SystemViewLogItem | null>(null)
let importAbort: AbortController | null = null
let replayFile: File | null = null
let replayLastTime: number | null = null
let replayWake: (() => void) | null = null
const replayState = ref<'idle' | 'playing' | 'paused' | 'ended'>('idle')
const replaySpeed = ref(1)
const replayProgress = ref(0)
const recordingBusy = ref(false)
let connectTimer: ReturnType<typeof setTimeout> | null = null
let mounted = false
let operationGeneration = 0
const SYSTEMVIEW_CHANNEL = 1
const RTT_SEARCH_SIZE = 1024

function persistSettings(next: DesktopSettings): void {
  settings.value = saveDesktopSettings(desktopStorage, next)
}

function isRttAddress(value: string): boolean {
  return /^0x[0-9a-f]{1,8}$/i.test(value)
}

function syncRttAddressFromSettings(): void {
  const latest = loadDesktopSettings(desktopStorage)
  settings.value = latest
  if (latest.rttAddress !== rttAddress.value) {
    rttAddress.value = latest.rttAddress
    addressError.value = ''
    addressSource.value = ''
  }
}

function onAddressInput(): void {
  addressError.value = ''
  addressSource.value = ''
  const address = rttAddress.value.trim()
  if (isRttAddress(address)) {
    persistSettings({ ...settings.value, rttAddress: address })
  }
}

async function searchRttAddress(): Promise<void> {
  if (searching.value || starting.value) return
  searching.value = true
  addressError.value = ''
  const latest = loadDesktopSettings(desktopStorage)
  settings.value = latest
  const source = latest.symbolPath.trim() || latest.mapPath.trim() || undefined
  try {
    const result = await findRtt(source)
    if (!result.addr || !isRttAddress(result.addr)) {
      throw new Error(result.details?.join(tr('；', '; ')) || result.warnings?.join(tr('；', '; ')) || tr('未找到 RTT 地址', 'RTT address not found'))
    }
    rttAddress.value = result.addr
    addressSource.value = result.source || (source ? tr('所选文件', 'Selected file') : tr('工程自动检测', 'Project auto-detection'))
    persistSettings({ ...latest, rttAddress: result.addr })
  } catch (caught) {
    addressError.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    searching.value = false
  }
}

// ---- 交互式 canvas 时间轴 ----
const tlCanvas = ref<HTMLCanvasElement | null>(null)
const tlTip = ref<HTMLDivElement | null>(null)
const tlLegend = ref<HTMLDivElement | null>(null)
let tlInstance: SvTimeline | null = null
let renderScheduler: RenderScheduler | null = null
const timelineFrameRate = new AdaptiveFrameRateController()
// Keep the initial fill cadence stable. The viewport still follows the newest
// event; only the intentional 60/30/20 FPS adaptation waits until one full
// timeline window is available.
const TIMELINE_STARTUP_FRAME_RATE = 30
let timelineStartupLocked = true
let timelineVisibleItemCount = 0
let visibleRequestId = 0
let visibleRequestInFlight = false
let visibleRequestPending = false
let latestBinaryTime: number | null = null
let binaryTickOrigin = 0n
let lastTableUpdate = Number.NEGATIVE_INFINITY
const TABLE_UPDATE_INTERVAL_MS = 200

function tlGetIntervals() {
  const visible = meta.cpuFreq
    ? filterRangesByWindow(intervals.value, lastT, windowUs.value)
    : intervals.value
  return visible.map(it => ({
    tid: it.taskId,
    name: taskStats[it.taskId]?.name || meta.taskNames[it.taskId] || ('0x' + (it.taskId >>> 0).toString(16).toUpperCase()),
    type: taskStats[it.taskId]?.type || 'Task',
    start: it.start, end: it.end, startTk: it.startTk, endTk: it.endTk,
  }))
}
function tlGetContexts() {
  const order: Record<string, number> = { ISR: 0, Scheduler: 1, Task: 2, Idle: 3 }
  return Object.values(taskStats)
    .sort((a, b) => (order[a.type || 'Task'] ?? 2) - (order[b.type || 'Task'] ?? 2))
    .map(context => ({ tid: context.id, name: context.name, type: context.type || 'Task' }))
}
function tlReset() { tlInstance?.reset() }

function requestTimelineVisibleRange(
  start: number,
  end: number,
  pixelWidth: number,
): void {
  // HPM traces can make Worker range extraction slower than one frame. Keep
  // only the newest request so the Worker queue cannot grow into bursty UI.
  visibleRequestPending = true
  if (visibleRequestInFlight) return
  visibleRequestInFlight = true
  visibleRequestPending = false
  binaryStream.requestVisibleRange(++visibleRequestId, start, end, pixelWidth)
}

function intervalCacheKey(interval: TaskInterval): string {
  const start = interval.startTk ?? interval.start
  return `${interval.taskId}:${String(start)}`
}

function mergeVisibleIntervals(next: TaskInterval[], latestTime: number): TaskInterval[] {
  const retainAfter = latestTime - ANALYSIS_BUFFER_US
  for (const interval of next) {
    if (interval.end >= retainAfter) visibleIntervalCache.set(intervalCacheKey(interval), interval)
  }
  for (const [key, interval] of visibleIntervalCache) {
    if (interval.end < retainAfter) visibleIntervalCache.delete(key)
  }
  if (visibleIntervalCache.size > MAX_INTERVALS) {
    const oldest = [...visibleIntervalCache.entries()]
      .sort((a, b) => a[1].end - b[1].end)
      .slice(0, visibleIntervalCache.size - MAX_INTERVALS)
    for (const [key] of oldest) visibleIntervalCache.delete(key)
  }
  return [...visibleIntervalCache.values()].sort((a, b) => a.start - b.start || a.end - b.end)
}

function resetTimelineRefreshPolicy(): void {
  timelineStartupLocked = true
  timelineFrameRate.reset()
}

function timelineWindowFilled(): boolean {
  if (latestBinaryTime === null || !Number.isFinite(latestBinaryTime)) return false
  const followSpan = tlInstance?.getFollowSpan?.() || windowUs.value
  const tickScale = meta.cpuFreq ? 1_000_000 / meta.cpuFreq : 1
  const windowTicks = meta.cpuFreq ? followSpan / tickScale : followSpan
  return latestBinaryTime >= windowTicks
}

function observeTimelineFrameRate(
  now: number,
  renderCostMs: number,
  pixelWidth: number,
): number {
  if (offlineMode.value) timelineStartupLocked = false
  if (timelineStartupLocked && timelineWindowFilled()) {
    timelineStartupLocked = false
    timelineFrameRate.reset()
  }
  if (timelineStartupLocked) return TIMELINE_STARTUP_FRAME_RATE
  return timelineFrameRate.observe({
    now,
    renderCostMs,
    visibleItems: timelineVisibleItemCount,
    pixelWidth,
  })
}

onMounted(() => {
  mounted = true
  window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, syncRttAddressFromSettings)
  const generation = ++operationGeneration
  refreshLogList()
  if (tlCanvas.value && tlTip.value && tlLegend.value) {
    tlInstance = new SvTimeline(
      { canvas: tlCanvas.value, tooltip: tlTip.value, legend: tlLegend.value },
      {
        intervals: tlGetIntervals(),
        unit: meta.cpuFreq ? 'us' : 'tk',
        tickHz: meta.cpuFreq || undefined,
        tickOrigin: binaryTickOrigin,
        follow: true,
        windowSize: windowUs.value,
        renderPaused: renderPaused.value,
        emptyText: tr('窗口内无任务', 'No tasks in this window'),
      },
    )
  }
  renderScheduler = new RenderScheduler((reasons) => {
    // The timeline itself interpolates toward the newest follow range. Keep
    // this repaint loop independent from worker delivery so the ruler moves
    // every frame instead of jumping once per data batch.
    const renderStarted = performance.now()
    tlInstance?.renderFrame(renderStarted)
    const nextFrameRate = observeTimelineFrameRate(
      renderStarted,
      performance.now() - renderStarted,
      tlCanvas.value?.clientWidth || 800,
    )
    renderScheduler?.setFrameRate(nextFrameRate)
    if (!reasons.has('data') && !reasons.has('zoom') && !reasons.has('resize')) return
    if (offlineMode.value) {
      if (reasons.has('data')) tlInstance?.setData(tlGetIntervals())
      return
    }
    const manualView = tlInstance && !tlInstance.follow ? tlInstance.getViewRange?.() : null
    const tickScale = meta.cpuFreq ? 1_000_000 / meta.cpuFreq : 1
    if (manualView) {
      // Keep a zoomed/panned frame stable while continuing to acquire and
      // repaint intervals that fall inside that frame.
      requestTimelineVisibleRange(
        Math.max(0, Math.floor(manualView.start / tickScale)),
        Math.max(0, Math.ceil(manualView.end / tickScale)),
        tlCanvas.value?.clientWidth || 800,
      )
      return
    }
    const end = latestBinaryTime ?? Number.MAX_SAFE_INTEGER
    const followSpanUs = tlInstance?.getFollowSpan?.() || windowUs.value
    const windowTicks = meta.cpuFreq
      ? followSpanUs * meta.cpuFreq / 1_000_000
      : followSpanUs
    const start = latestBinaryTime === null ? 0 : Math.max(0, end - windowTicks)
    requestTimelineVisibleRange(start, end, tlCanvas.value?.clientWidth || 800)
  }, undefined, undefined, { frameRate: TIMELINE_STARTUP_FRAME_RATE, continuous: true })
  renderScheduler.start()
  reconnectRunningTrace(generation)
})
onUnmounted(() => {
  mounted = false
  operationGeneration++
  window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, syncRttAddressFromSettings)
  abortImport()
  cancelPendingConnect()
  disconnectStatus()
  binaryStream.stop()
  visibleRequestInFlight = false
  visibleRequestPending = false
  renderScheduler?.dispose()
  renderScheduler = null
  tlInstance?.destroy()
  tlInstance = null
})

function scheduleTimelineFlush() {
  renderScheduler?.invalidate('data')
}

function cancelPendingConnect() {
  if (connectTimer !== null) {
    clearTimeout(connectTimer)
    connectTimer = null
  }
}

function operationIsActive(generation: number): boolean {
  return mounted && generation === operationGeneration
}
watch(intervals, () => { if (offlineMode.value) scheduleTimelineFlush() })
watch(windowUs, () => {
  tlInstance?.setWindowSize(windowUs.value)
  scheduleTimelineFlush()
})
watch(language, () => tlInstance?.setLabels({ emptyText: tr('窗口内无任务', 'No tasks in this window') }))
// cpuFreq 变了切换单位（重建）
watch(() => meta.cpuFreq, () => {
  if (tlCanvas.value && tlTip.value && tlLegend.value) {
    tlInstance?.destroy()
    tlInstance = new SvTimeline(
      { canvas: tlCanvas.value, tooltip: tlTip.value, legend: tlLegend.value },
      {
        intervals: tlGetIntervals(),
        unit: meta.cpuFreq ? 'us' : 'tk',
        tickHz: meta.cpuFreq || undefined,
        tickOrigin: binaryTickOrigin,
        follow: true,
        windowSize: windowUs.value,
        renderPaused: renderPaused.value,
        emptyText: tr('窗口内无任务', 'No tasks in this window'),
      },
    )
  }
})

const MAX_EVENTS = 800
const ANALYSIS_EVENTS = 100_000
const MAX_INTERVALS = 50_000
const ANALYSIS_BUFFER_US = 60_000_000

function taskNameFromMeta(id: number): string {
  return normalizeSystemViewName(meta.taskNames[id] || meta.taskNames[String(id) as any])
}

function isrNameFromMeta(id: number): string {
  return normalizeSystemViewName(meta.isrNames[id] || meta.isrNames[String(id) as any])
}

function applyTaskNames(names: Record<number, unknown>) {
  const cleanNames: Record<number, string> = {}
  for (const [idText, rawName] of Object.entries(names)) {
    const id = Number(idText)
    const name = normalizeSystemViewName(rawName)
    if (Number.isFinite(id) && name) cleanNames[id] = name
  }
  meta.taskNames = cleanNames
  for (const [idText, name] of Object.entries(cleanNames)) {
    ensureContext(1, Number(idText)).name = name
  }
}

function applyIsrNames(names: Record<number, unknown>) {
  const cleanNames: Record<number, string> = {}
  for (const [idText, rawName] of Object.entries(names)) {
    const id = Number(idText)
    const name = normalizeSystemViewName(rawName)
    if (!Number.isFinite(id) || !name) continue
    cleanNames[id] = name
    const laneId = contextLaneId(2, id)
    if (taskStats[laneId]) taskStats[laneId].name = name
  }
  meta.isrNames = cleanNames
}

function colorFor(id: number): string {
  if (taskStats[id]) return taskStats[id].color
  const idx = Object.keys(taskStats).length % PALETTE.length
  return PALETTE[idx]
}

function contextLaneId(type: number, id: number): number {
  if (type === 1) return id >>> 0
  if (type === 2) return (0x80000000 | (id & 0x7fffffff)) >>> 0
  if (type === 3) return 0xfffffffe
  return 0xffffffff
}

function contextTypeName(type: number): string {
  if (type === 2) return 'ISR'
  if (type === 3) return 'Scheduler'
  if (type === 4) return 'Idle'
  return 'Task'
}

function contextDisplayName(type: number, id: number): string {
  if (type === 2) return isrNameFromMeta(id) || `ISR #${id}`
  if (type === 3) return 'Scheduler'
  if (type === 4) return 'Idle'
  return taskNameFromMeta(id) || hexId(id)
}

function ensureContext(type: number, rawId: number): TaskStat {
  const laneId = contextLaneId(type, rawId)
  const resolvedName = contextDisplayName(type, rawId)
  if (!taskStats[laneId]) {
    taskStats[laneId] = {
      id: laneId,
      rawId: rawId >>> 0,
      name: resolvedName,
      color: colorFor(laneId),
      runUs: 0,
      switches: 0,
      type: contextTypeName(type),
    }
  } else if (resolvedName) {
    taskStats[laneId].name = resolvedName
  }
  return taskStats[laneId]
}

function ensureTask(id: number, name?: string): TaskStat {
  const cleanName = normalizeSystemViewName(name)
  if (!taskStats[id]) {
    taskStats[id] = {
      id,
      rawId: id,
      name: cleanName || taskNameFromMeta(id),
      color: colorFor(id),
      runUs: 0,
      switches: 0,
      type: 'Task',
    }
  }
  const resolvedName = cleanName || taskNameFromMeta(id)
  if (resolvedName && !taskStats[id].name) taskStats[id].name = resolvedName
  return taskStats[id]
}

function tOf(e: any): number {
  // 优先用 µs（已按 CPUFreq 换算），否则 ticks
  if (typeof e.t_us === 'number') return e.t_us
  if (typeof e.t_ticks === 'number' && meta.cpuFreq > 0) {
    return e.t_ticks * 1_000_000 / meta.cpuFreq
  }
  return e.t_ticks ?? 0
}

function tickOf(e: any): number | undefined {
  return typeof e.t_ticks === 'number' ? e.t_ticks : undefined
}

function ingestEvents(events: any[], countEvents = true) {
  const normalizedEvents = events.map(e => ({ ...e, t: tOf(e), tk: tickOf(e) }))

  for (const e of normalizedEvents) {
    const t = e.t
    if (t > 0) {
      if (firstT === 0) firstT = t
      if (t > lastT) lastT = t
    }
    const k = e.kind
    if (k === 'idle') {
      // idle 周期：用 delta 累计（粗略）
      if (typeof e.cpu_delta_us === 'number') idleUs.value += e.cpu_delta_us
    }
  }

  const newIntervals = ingestSystemViewIntervals(normalizedEvents, intervalState, {
    ensureTask: (id, name) => ensureTask(id, name),
    addRunTime: (id, duration) => { ensureTask(id).runUs += duration },
    addSwitch: id => { ensureTask(id).switches++ },
    applyTaskInfo: (id, event) => {
      if (event.prio !== undefined) ensureTask(id).prio = event.prio
    },
  })

  if (countEvents) totalEventCount.value += events.length
  if (normalizedEvents.length) {
    eventList.value = appendManyToLast(eventList.value, normalizedEvents, MAX_EVENTS)
    analysisEvents = appendAndTrimEventsByTime(
      analysisEvents,
      normalizedEvents,
      lastT,
      ANALYSIS_BUFFER_US,
      ANALYSIS_EVENTS,
      event => event.t,
    )
    analysisBufferCount.value = analysisEvents.length
  }
  if (newIntervals.length) {
    intervals.value = appendAndTrimRanges(
      intervals.value,
      newIntervals,
      lastT,
      ANALYSIS_BUFFER_US,
      MAX_INTERVALS,
    )
  }
}

async function reconnectRunningTrace(generation: number) {
  if (offlineMode.value) return
  try {
    const status = await dash.getStatus()
    if (!operationIsActive(generation)) return
    if (status?.running) {
      applyTargetOverflowStatus(status)
      if (status.synced !== undefined) meta.synced = !!status.synced
      if (status.cpu_freq !== undefined) meta.cpuFreq = Number(status.cpu_freq) || 0
      if (status.cpu_freq_source !== undefined) meta.cpuFreqSource = status.cpu_freq_source || ''
      if (status.dropped_bytes !== undefined) {
        meta.sessionDropped = Number(status.dropped_bytes || 0) + Number(status.dropped_packets || 0)
      }
      if (status.recording !== undefined) meta.recording = !!status.recording
      if (status.recording_path) meta.recordingPath = status.recording_path
      if (status.recording_summary_path) meta.recordingSummaryPath = status.recording_summary_path
      if (status.recording_error !== undefined) meta.recordingError = status.recording_error || ''
      if (status.task_names) applyTaskNames(status.task_names)
      if (status.isr_names) applyIsrNames(status.isr_names)
      await dash.start()
      if (!operationIsActive(generation)) return
      connectStatus()
      binaryStream.start()
    }
  } catch {
    // Best effort: opening the tab should not surface a stale-status error.
  }
}

watch(statusData, (nw) => {
  if (offlineMode.value) return
  const fresh = takeNewStreamPoints(nw as any[], lastStreamSeq)
  for (const dp of fresh.points as any[]) {
    const evt = dp.event || dp._event
    applyTargetOverflowStatus(dp)
    if (dp.synced !== undefined) meta.synced = !!dp.synced
    if (dp.dropped_bytes !== undefined) meta.sessionDropped = dp.dropped_bytes + (dp.dropped_packets || 0)
    if (dp.runtime_dropped_bytes !== undefined || dp.dropped_bytes !== undefined) {
      meta.dropped = (dp.runtime_dropped_bytes ?? dp.dropped_bytes ?? 0) + (dp.dropped_packets || 0)
    }
    if (dp.cpu_freq !== undefined) meta.cpuFreq = dp.cpu_freq
    if (dp.cpu_freq_source !== undefined) meta.cpuFreqSource = dp.cpu_freq_source || ''
    if (dp.recording !== undefined) meta.recording = !!dp.recording
    if (dp.recording_path !== undefined) meta.recordingPath = dp.recording_path || meta.recordingPath
    if (dp.recording_summary_path !== undefined) meta.recordingSummaryPath = dp.recording_summary_path || meta.recordingSummaryPath
    if (dp.recording_error !== undefined) meta.recordingError = dp.recording_error || ''
    if (dp.progress_state === 'error' && dp.progress_error) {
      runtimeError.value = String(dp.progress_error)
    }
    const backendEvents = Number(dp.stats?.events)
    if (Number.isFinite(backendEvents)) totalEventCount.value = Math.max(totalEventCount.value, backendEvents)
    if (dp.task_names) applyTaskNames(dp.task_names)
    if (dp.isr_names) applyIsrNames(dp.isr_names)
    if (evt !== 'status') continue
  }
  lastStreamSeq = fresh.nextSeq
})

watch(binaryStream.telemetry, telemetry => {
  if (!telemetry || offlineMode.value) return
  renderScheduler?.recordCollection(telemetry.bufferedSamples)
  renderScheduler?.invalidate('data')
})

// The first live frame can arrive after the scheduler's initial range request.
// Before the decoder sees a SystemView record it answers that request with the
// generic render-envelope shape. Release the request gate so the next frame
// can ask again once SystemView mode is active.
watch(binaryStream.envelope, envelope => {
  if (!envelope || offlineMode.value || !visibleRequestInFlight) return
  visibleRequestInFlight = false
  renderScheduler?.invalidate('data')
})

watch(binaryStream.error, error => {
  if (error) runtimeError.value = `SystemView stream: ${error}`
})

watch(binaryStream.systemViewVisible, visible => {
  if (!visible) return
  if (renderPaused.value || offlineMode.value || visible.requestId !== visibleRequestId) return
  visibleRequestInFlight = false
  latestBinaryTime = visible.latestTime
  // candidateIntervalCount is the Worker scan cost, not the amount painted.
  // HPM traces contain many 1 ms ISR intervals; using the scan count here
  // incorrectly forces the live canvas to 20/30 FPS even when painting is
  // cheap. The controller also observes measured paint cost below.
  timelineVisibleItemCount = Math.max(
    visible.intervalCount,
    visible.eventCount,
  )
  binaryTickOrigin = visible.tickOrigin
  const tickScale = meta.cpuFreq ? 1_000_000 / meta.cpuFreq : 1
  const taskIds = new Uint32Array(visible.taskIds)
  const contextTypes = visible.contextTypes
    ? new Uint8Array(visible.contextTypes)
    : new Uint8Array(visible.intervalCount).fill(1)
  const starts = new Float64Array(visible.starts)
  const ends = new Float64Array(visible.ends)
  const startTicks = new BigUint64Array(visible.startTicks)
  const endTicks = new BigUint64Array(visible.endTicks)
  const nextIntervals: TaskInterval[] = []
  for (let index = 0; index < visible.intervalCount; index++) {
    const context = ensureContext(contextTypes[index] || 1, taskIds[index])
    const start = starts[index] * tickScale
    const end = ends[index] * tickScale
    nextIntervals.push({
      taskId: context.id, start, end,
      startTk: startTicks[index], endTk: endTicks[index],
    })
  }

  for (const context of Object.values(taskStats)) {
    context.runUs = 0
    context.switches = 0
  }
  const summaries = visible.contexts || []
  const totalTicks = summaries.reduce((sum, context) => sum + context.totalTicks, 0)
  exactRuntimeRows.value = summaries.map(contextSummary => {
    const context = ensureContext(contextSummary.type, contextSummary.id)
    context.prio = contextSummary.priority
    context.stackBase = contextSummary.stackBase
    context.stackSize = contextSummary.stackSize
    context.runUs = contextSummary.totalTicks * tickScale
    context.switches = contextSummary.count
    return {
      ...context,
      count: contextSummary.count,
      minUs: contextSummary.minTicks * tickScale,
      p25Us: contextSummary.p25Ticks * tickScale,
      p50Us: contextSummary.p50Ticks * tickScale,
      p75Us: contextSummary.p75Ticks * tickScale,
      maxUs: contextSummary.maxTicks * tickScale,
      totalUs: contextSummary.totalTicks * tickScale,
      pct: totalTicks > 0 ? contextSummary.totalTicks / totalTicks * 100 : 0,
    }
  }).sort((a, b) => b.totalUs - a.totalUs || a.name.localeCompare(b.name))
  intervals.value = mergeVisibleIntervals(nextIntervals, visible.latestTime * tickScale)
  lastT = visible.latestTime * tickScale
  tlInstance?.setTickOrigin(binaryTickOrigin)
  tlInstance?.setContexts?.(tlGetContexts(), { render: false })
  tlInstance?.setPrefilteredIntervals(tlGetIntervals())
  if (visibleRequestPending) renderScheduler?.invalidate('data')

  const now = performance.now()
  if (now - lastTableUpdate >= TABLE_UPDATE_INTERVAL_MS) {
    lastTableUpdate = now
    eventList.value = visible.events.map(event => ({
      ...event,
      task_name: event.task_id === undefined ? undefined : taskNameFromMeta(event.task_id),
      isr_name: event.isr_id === undefined ? undefined : isrNameFromMeta(event.isr_id),
      t: (event.t_relative ?? 0) * tickScale,
      tk: event.t_ticks,
      duration_us: event.duration_ticks === undefined
        ? undefined
        : event.duration_ticks * tickScale,
    }))
    analysisBufferCount.value = binaryStream.telemetry.value?.bufferedSamples || 0
  }
})

// ---- 计算属性 ----
const eventCount = computed(() => totalEventCount.value)
const taskCount = computed(() => Object.values(taskStats).filter(task => task.type === 'Task').length)

const tableEvents = computed(() => eventList.value.slice(-120))
const eventRows = computed(() => buildSystemViewEventRows(tableEvents.value, {
  firstIndex: Math.max(1, totalEventCount.value - tableEvents.value.length + 1),
  formatTime: value => fmtTime(value),
  preferExactTicks: !meta.cpuFreq,
}))
const runtimeRows = computed(() => offlineMode.value
  ? computeRuntimeRows(Object.values(taskStats), intervals.value)
  : exactRuntimeRows.value)
const contextRows = computed(() => computeContextRows(Object.values(taskStats)))
const currentJsonlPath = computed(() => meta.recordingPath || latestLog.value?.path || '')
const currentSummaryPath = computed(() => meta.recordingSummaryPath || latestLog.value?.summary_path || '')

// ---- 辅助 ----
function clamp(v: number) { return Math.max(0, Math.min(100, v)) }

function applyTargetOverflowStatus(status: Record<string, unknown>): void {
  const events = Number(status.target_overflow_events)
  const droppedPackets = Number(status.target_dropped_packets_since_baseline)
  const dropCount = Number(status.target_drop_count)
  if (Number.isFinite(events) && events >= 0) {
    meta.targetOverflowEvents = Math.max(meta.targetOverflowEvents, Math.trunc(events))
  }
  if (Number.isFinite(droppedPackets) && droppedPackets >= 0) {
    meta.targetDroppedPackets = Math.max(meta.targetDroppedPackets, Math.trunc(droppedPackets))
  }
  if (Number.isFinite(dropCount) && dropCount >= 0) {
    meta.targetDropCount = Math.trunc(dropCount)
  }
}

function hexId(id: number) { return '0x' + (id >>> 0).toString(16).toUpperCase() }
function fmtCpuFreq(freq: number) {
  return freq >= 1_000_000 ? (freq / 1_000_000).toFixed(0) + 'MHz' : freq.toLocaleString() + 'Hz'
}
function fmtTime(t: any) {
  if (typeof t === 'number' && meta.cpuFreq) {
    const seconds = t / 1_000_000
    return seconds.toFixed(Math.abs(seconds) < 0.001 ? 9 : 6) + 's'
  }
  if (typeof t === 'number') return Math.round(t).toLocaleString() + ' tk'
  return ''
}
function fmtDurationUs(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '-'
  if (value >= 1_000_000) return (value / 1_000_000).toFixed(3) + 's'
  if (value >= 1_000) return (value / 1_000).toFixed(2) + 'ms'
  return Math.round(value).toLocaleString() + 'us'
}
function evtColor(k: string) {
  if (k.startsWith('task_start')) return 'c-start'
  if (k.startsWith('task_stop')) return 'c-stop'
  if (k.startsWith('isr')) return 'c-isr'
  if (k === 'idle') return 'c-idle'
  return ''
}
function clearAll() {
  binaryStream.reset()
  visibleRequestId++
  visibleRequestInFlight = false
  visibleRequestPending = false
  eventList.value = []
  analysisEvents = []
  analysisBufferCount.value = 0
  intervals.value = []
  visibleIntervalCache.clear()
  exactRuntimeRows.value = []
  Object.keys(taskStats).forEach(k => delete taskStats[Number(k)])
  intervalState = { currentTaskId: null, currentStart: null }
  totalEventCount.value = 0
  idleUs.value = 0; firstT = 0; lastT = 0; lastStreamSeq = 0
  latestBinaryTime = null
  resetTimelineRefreshPolicy()
  binaryTickOrigin = 0n
  lastTableUpdate = Number.NEGATIVE_INFINITY
  meta.synced = false
  meta.dropped = 0
  meta.sessionDropped = 0
  meta.targetOverflowEvents = 0
  meta.targetDroppedPackets = 0
  meta.targetDropCount = null
  meta.cpuFreq = 0
  meta.cpuFreqSource = ''
  meta.taskNames = {}
  meta.isrNames = {}
  meta.recording = false
  meta.recordingPath = ''
  meta.recordingSummaryPath = ''
  meta.recordingError = ''
}

async function refreshLogList() {
  try {
    const res = await fetch(`${API_BASE}/api/dash/systemview/logs`)
    if (!res.ok) return
    const body = await res.json()
    latestLog.value = Array.isArray(body.logs) ? body.logs[0] || null : null
  } catch {
    latestLog.value = null
  }
}

function exportLog(path: string) {
  if (!path) return
  window.open(`${API_BASE}/api/dash/systemview/logs/download?path=${encodeURIComponent(path)}`, '_blank')
}

async function toggleRecording() {
  if (recordingBusy.value || offlineMode.value) return
  recordingBusy.value = true
  runtimeError.value = null
  try {
    const action = meta.recording ? 'stop' : 'start'
    const response = await fetch(`${API_BASE}/api/dash/systemview/recording/${action}`, { method: 'POST' })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || tr('保存操作失败', 'Recording operation failed'))
    meta.recording = !!body.recording
    if (body.recording_path) meta.recordingPath = body.recording_path
    if (body.recording_summary_path) meta.recordingSummaryPath = body.recording_summary_path
    meta.recordingError = body.recording_error || ''
    if (!meta.recording) await refreshLogList()
  } catch (caught) {
    runtimeError.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    recordingBusy.value = false
  }
}

function triggerImport() {
  fileInput.value?.click()
}

async function onImportFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  await importLogFile(file)
}

async function importLogFile(file: File) {
  operationGeneration++
  abortImport()
  cancelPendingConnect()
  const controller = new AbortController()
  importAbort = controller
  disconnectStatus()
  binaryStream.stop()
  renderPaused.value = false
  tlInstance?.resumeRendering()
  renderScheduler?.start()
  if (dash.state.value !== 'idle') {
    await dash.stop()
  }
  clearAll()
  replayFile = file
  replayLastTime = null
  replayState.value = 'playing'
  replayProgress.value = 0
  offlineMode.value = true
  offlineFileName.value = file.name
  importStatus.value = tr('准备回放', 'Preparing replay')
  importError.value = false
  meta.synced = true

  try {
    const result = await importSystemViewJsonl({
      stream: file.stream(),
      batchSize: 500,
      signal: controller.signal,
      onSession: record => applyImportedMeta(record),
      onSummary: record => applyImportedMeta(record),
      onProgress: bytesRead => {
        replayProgress.value = file.size > 0 ? Math.min(100, bytesRead / file.size * 100) : 100
      },
      onBatch: async events => {
        await paceReplayBatch(events, controller.signal)
        ingestEvents(events, true)
        importStatus.value = tr(`回放中 ${totalEventCount.value.toLocaleString()}`, `Replaying ${totalEventCount.value.toLocaleString()}`)
      },
    })
    if (importAbort !== controller) return
    const suffix = result.parseErrors || result.skipped
      ? tr(`，跳过 ${result.skipped.toLocaleString()}，错误 ${result.parseErrors.toLocaleString()}`, `, skipped ${result.skipped.toLocaleString()}, errors ${result.parseErrors.toLocaleString()}`)
      : ''
    replayState.value = 'ended'
    replayProgress.value = 100
    importStatus.value = tr(`回放完成 ${result.events.toLocaleString()}${suffix}`, `Replay complete ${result.events.toLocaleString()}${suffix}`)
    importError.value = result.parseErrors > 0
  } catch (e) {
    if (importAbort !== controller) return
    importError.value = !isAbortError(e)
    if (!isAbortError(e)) replayState.value = 'ended'
    importStatus.value = isAbortError(e)
      ? tr('已取消', 'Canceled')
      : tr(`导入失败：${e instanceof Error ? e.message : String(e)}`, `Import failed: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    if (importAbort === controller) importAbort = null
    scheduleTimelineFlush()
  }
}

function replayEventTime(event: any): number | null {
  if (typeof event?.t_us === 'number' && Number.isFinite(event.t_us)) return event.t_us
  if (typeof event?.t_ticks === 'number' && Number.isFinite(event.t_ticks) && meta.cpuFreq > 0) {
    return event.t_ticks * 1_000_000 / meta.cpuFreq
  }
  return null
}

async function paceReplayBatch(events: any[], signal: AbortSignal) {
  await waitForReplayResume(signal)
  const last = [...events].reverse().map(replayEventTime).find(value => value !== null) ?? null
  if (last !== null && replayLastTime !== null && last >= replayLastTime) {
    await waitReplayDelay((last - replayLastTime) / Math.max(0.1, replaySpeed.value), signal)
  }
  await waitForReplayResume(signal)
  if (last !== null) replayLastTime = last
}

async function waitForReplayResume(signal: AbortSignal) {
  while (replayState.value === 'paused') {
    await new Promise<void>((resolve, reject) => {
      const onAbort = () => {
        replayWake = null
        reject(new Error('SystemView import aborted'))
      }
      replayWake = () => {
        signal.removeEventListener('abort', onAbort)
        replayWake = null
        resolve()
      }
      signal.addEventListener('abort', onAbort, { once: true })
    })
  }
  if (signal.aborted) throw new Error('SystemView import aborted')
}

async function waitReplayDelay(delayUs: number, signal: AbortSignal) {
  let remainingMs = Math.max(0, delayUs / 1000)
  while (remainingMs > 0) {
    const chunkMs = Math.min(remainingMs, 100)
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        signal.removeEventListener('abort', onAbort)
        resolve()
      }, chunkMs)
      const onAbort = () => {
        clearTimeout(timer)
        reject(new Error('SystemView import aborted'))
      }
      signal.addEventListener('abort', onAbort, { once: true })
    })
    remainingMs -= chunkMs
    await waitForReplayResume(signal)
  }
}

function toggleReplay() {
  if (replayState.value === 'playing') {
    replayState.value = 'paused'
  } else if (replayState.value === 'paused') {
    replayState.value = 'playing'
    replayWake?.()
  }
}

function restartReplay() {
  const file = replayFile
  if (file) void importLogFile(file)
}

function applyImportedMeta(record: Record<string, unknown>) {
  applyTargetOverflowStatus(record)
  const cpuFreq = Number(record.cpu_freq)
  if (Number.isFinite(cpuFreq) && cpuFreq > 0) meta.cpuFreq = cpuFreq
  if (typeof record.cpu_freq_source === 'string') meta.cpuFreqSource = record.cpu_freq_source
  const droppedBytes = Number(record.dropped_bytes)
  const droppedPackets = Number(record.dropped_packets)
  if (Number.isFinite(droppedBytes) || Number.isFinite(droppedPackets)) {
    const runtimeDropped = Number(record.runtime_dropped_bytes)
    meta.sessionDropped = Math.max(0, droppedBytes || 0) + Math.max(0, droppedPackets || 0)
    meta.dropped = Math.max(0, Number.isFinite(runtimeDropped) ? runtimeDropped : droppedBytes || 0) + Math.max(0, droppedPackets || 0)
  }
  if (record.task_names && typeof record.task_names === 'object' && !Array.isArray(record.task_names)) {
    applyTaskNames(record.task_names as Record<number, unknown>)
  }
  if (record.isr_names && typeof record.isr_names === 'object' && !Array.isArray(record.isr_names)) {
    meta.isrNames = record.isr_names as Record<number, string>
  }
}

function abortImport() {
  if (importAbort) {
    importAbort.abort()
    importAbort = null
  }
  replayWake?.()
  replayWake = null
}

function returnToLive() {
  abortImport()
  offlineMode.value = false
  offlineFileName.value = ''
  importStatus.value = ''
  importError.value = false
  replayState.value = 'idle'
  replayProgress.value = 0
  replayLastTime = null
  clearAll()
  refreshLogList()
}

function isAbortError(value: unknown): boolean {
  return value instanceof Error && /aborted/i.test(value.message)
}

async function waitForSystemViewReady(generation: number, timeoutMs = 8_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs
  while (operationIsActive(generation) && Date.now() < deadline) {
    try {
      const status = await dash.getStatus()
      if (!operationIsActive(generation)) return false
      if (status?.progress_state === 'error') {
        runtimeError.value = status.progress_error || tr('SystemView 启动失败', 'SystemView startup failed')
        return false
      }
      if (status?.progress_state === 'streaming' || Number(status?.stats?.bytes) > 0) {
        return true
      }
      if (status?.running === false && status?.progress_state !== 'starting') {
        runtimeError.value = status?.progress_error || tr('SystemView 会话已停止', 'SystemView session stopped')
        return false
      }
    } catch (caught) {
      runtimeError.value = caught instanceof Error ? caught.message : String(caught)
      return false
    }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  if (operationIsActive(generation)) {
    runtimeError.value = tr(
      'SystemView 启动超时，请检查 RTT 地址、通道和下位机配置',
      'SystemView startup timed out. Check the RTT address, channel, and target configuration',
    )
  }
  return false
}

async function onStart() {
  if (searching.value || starting.value) return
  if (!props.deviceConnected) {
    runtimeError.value = tr('请先连接 MKLink 设备', 'Connect the MKLink device first')
    return
  }
  const generation = ++operationGeneration
  abortImport()
  cancelPendingConnect()
  offlineMode.value = false
  offlineFileName.value = ''
  importStatus.value = ''
  importError.value = false
  latestLog.value = null
  runtimeError.value = null
  const latest = loadDesktopSettings(desktopStorage)
  settings.value = latest
  const address = rttAddress.value.trim() || latest.rttAddress.trim()
  if (!isRttAddress(address)) {
    addressError.value = tr('请输入有效的 RTT 地址，或先执行自动搜索', 'Enter a valid RTT address or run Auto Search first')
    return
  }
  rttAddress.value = address
  persistSettings({ ...latest, rttAddress: address })
  const conflicts = await checkConflict('systemview')
  if (!operationIsActive(generation)) return
  if (conflicts.length > 0) {
    const names = conflicts.map(c => c).join('、')
    if (!confirm(tr(`启动 SystemView 将停止当前运行的 ${names} 会话。确认？`, `Starting SystemView will stop the active ${names} session. Continue?`))) return
  }
  clearAll()
  renderPaused.value = false
  tlInstance?.resumeRendering()
  renderScheduler?.start()
  starting.value = true
  try {
    const started = await dash.start({
      addr: address,
      channel: SYSTEMVIEW_CHANNEL,
      mode: 0,
      search_size: RTT_SEARCH_SIZE,
    })
    if (!started || !operationIsActive(generation)) return
    connectTimer = setTimeout(() => {
      connectTimer = null
      if (!operationIsActive(generation)) return
      connectStatus()
      binaryStream.start()
    }, 500)
    await waitForSystemViewReady(generation)
  } finally {
    if (operationIsActive(generation)) starting.value = false
  }
}
function onPauseRender() {
  renderPaused.value = true
  visibleRequestId++
  visibleRequestInFlight = false
  visibleRequestPending = false
  renderScheduler?.stop()
  tlInstance?.pauseRendering()
}
function onResumeRender() {
  renderPaused.value = false
  tlInstance?.resumeRendering()
  renderScheduler?.start()
  renderScheduler?.invalidate('data')
}
async function onStop() {
  const generation = ++operationGeneration
  cancelPendingConnect()
  disconnectStatus()
  binaryStream.stop()
  renderPaused.value = false
  tlInstance?.resumeRendering()
  renderScheduler?.start()
  await dash.stop()
  if (!operationIsActive(generation)) return
  runtimeError.value = null
  meta.recording = false
  await refreshLogList()
}
</script>

<style scoped>
.sv-tab { display: flex; flex-direction: column; height: 100%; gap: 8px; }
.alert-warn { color: var(--warn); padding: 8px; border: 1px solid var(--warn); border-radius: 4px; }
.sv-toolbar { display: flex; align-items: center; gap: 12px; padding: 6px 0; flex-wrap: wrap; }
.sv-address-row { display: grid; grid-template-columns: auto minmax(180px, 320px) auto minmax(0, 1fr); align-items: center; gap: 8px; padding: 4px 0; }
.sv-address-row label { font-size: 12px; color: var(--muted); }
.sv-address-row input { min-width: 0; height: 30px; padding: 0 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: inherit; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.btn-search { height: 30px; display: inline-flex; align-items: center; gap: 5px; padding: 0 9px; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: inherit; cursor: pointer; }
.btn-search:disabled { opacity: .45; cursor: not-allowed; }
.address-error { min-width: 0; color: var(--danger, #dc2626); font-size: 12px; overflow-wrap: anywhere; }
.address-source { min-width: 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.sv-offline-toolbar { padding-bottom: 0; }
.sv-file-input { display: none; }
.sv-stat { font-size: 12px; color: var(--muted); }
.sv-stat b { color: var(--fg); }
.sv-stat.warn { color: var(--warn); }
.sv-window { font-size: 12px; color: var(--muted); margin-left: auto; display: flex; align-items: center; gap: 4px; }
.sv-window select { background: var(--field-bg); color: var(--fg); border: 1px solid var(--control-border); border-radius: 4px; padding: 2px 4px; }
.sv-section { border: 1px solid var(--border); border-radius: var(--radius); padding: 8px; }
.sv-section-title { font-size: 12px; color: var(--muted); margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
.sv-section-title > span:first-child { color: var(--fg); font-weight: 650; }
.sv-section-subtitle { color: var(--dim); font-size: 11px; font-weight: 400; }
.btn-clear { margin-left: auto; background: none; border: 1px solid var(--border); border-radius: 4px; color: var(--muted); font-size: 11px; padding: 1px 8px; cursor: pointer; }
.btn-clear:disabled { opacity: .45; cursor: not-allowed; }
.sv-tool-btn,
.sv-mode-btn { margin-left: 0; }
.sv-record-btn,
.sv-replay-command { display: inline-flex; align-items: center; gap: 5px; }
.sv-record-btn.active { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 52%, var(--border)); background: var(--danger-bg); }
.sv-replay-bar { display: flex; align-items: center; gap: 9px; min-height: 32px; padding: 5px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--surface-muted); }
.sv-replay-command { margin-left: 0; padding: 3px 8px; }
.sv-replay-speed { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); font-size: 11px; }
.sv-replay-speed select { border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: inherit; padding: 2px 4px; }
.sv-replay-bar progress { flex: 1; min-width: 100px; height: 7px; accent-color: var(--brand); }
.sv-replay-progress { width: 46px; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; text-align: right; }
.sv-replay-status { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 11px; }
.sv-section-actions { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }
.sv-section-actions .btn-clear { margin-left: 0; }
.sv-empty { color: var(--dim); font-size: 12px; padding: 12px; text-align: center; }

.sv-health-grid { display: grid; grid-template-columns: repeat(6, minmax(118px, 1fr)); gap: 8px; }
.sv-health-card { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; }
.sv-health-card span { color: var(--dim); font-size: 11px; }
.sv-health-card b { color: var(--fg); font-size: 15px; font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sv-health-card.warn { border-color: color-mix(in srgb, var(--warn) 48%, var(--border)); background: var(--warn-bg); }
.sv-health-card.warn span,
.sv-health-card.warn b { color: var(--warn); }
.sv-health-wide { grid-column: span 2; }

/* 甘特 */
.sv-gantt-section { flex: 0 0 auto; min-height: 0; display: flex; flex-direction: column; }
.sv-legend { display: flex; gap: 5px; flex-wrap: wrap; align-content: flex-start; height: 26px; overflow-y: auto; scrollbar-gutter: stable; margin: 3px 0 5px; }
.sv-legend :deep(.sv-lg) { display: inline-flex; align-items: center; gap: 4px; background: var(--surface-muted); color: var(--fg); border: 1px solid var(--border); border-radius: 4px; padding: 2px 7px; font-size: 11px; cursor: pointer; user-select: none; }
.sv-legend :deep(.sv-lg i) { width: 3px; height: 11px; border-radius: 1px; display: inline-block; }
.sv-legend :deep(.sv-lg-off) { opacity: .4; text-decoration: line-through; }
.sv-canvas-wrap { position: relative; background: var(--surface-muted); border: 1px solid var(--border); border-radius: 4px; overflow: visible; }
.sv-canvas-wrap :deep(canvas) { display: block; width: 100%; cursor: grab; user-select: none; }
.sv-tip { position: fixed; display: none; background: #1c2128; border: 1px solid #444c56; border-radius: 6px; padding: 6px 10px; font-size: 11px; color: #f0f6fc; pointer-events: none; z-index: 99; font-family: var(--font-mono, monospace); white-space: nowrap; }

/* 事件列表 */
.sv-events-section { flex: 0 0 auto; display: flex; flex-direction: column; }
.sv-events-section.collapsed { flex: 0 0 auto; max-height: none; }
.sv-events-section.collapsed .sv-section-title { margin-bottom: 0; }
.sv-events-table-wrap { max-height: 190px; }
.sv-bottom-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr); gap: 8px; margin-top: 8px; }
.sv-runtime-section,
.sv-context-section { height: 150px; min-height: 0; display: flex; flex-direction: column; }
.sv-runtime-section .sv-table-wrap,
.sv-context-section .sv-table-wrap { flex: 1; min-height: 0; }
.sv-table-wrap { overflow: auto; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); }
.sv-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; font-variant-numeric: tabular-nums; }
.sv-table th { position: sticky; top: 0; z-index: 1; background: var(--surface-muted); color: var(--muted); font-weight: 650; text-align: left; border-bottom: 1px solid var(--border); padding: 5px 6px; white-space: nowrap; }
.sv-table td { border-bottom: 1px solid var(--line); color: var(--fg); padding: 4px 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sv-table tbody tr:nth-child(even) td { background: color-mix(in srgb, var(--surface-muted) 58%, transparent); }
.sv-table tbody tr:hover td { background: var(--surface-selected); }
.sv-events-table th:nth-child(1),
.sv-events-table td:nth-child(1) { width: 58px; text-align: right; }
.sv-events-table th:nth-child(2),
.sv-events-table td:nth-child(2) { width: 118px; }
.sv-events-table th:nth-child(3),
.sv-events-table td:nth-child(3) { width: 132px; }
.sv-events-table th:nth-child(4),
.sv-events-table td:nth-child(4) { width: 142px; }
.sv-events-table th:nth-child(5),
.sv-events-table td:nth-child(5) { width: 100px; }
.sv-name-cell { display: flex; align-items: center; gap: 6px; min-width: 0; }
.sv-name-cell i { width: 8px; height: 8px; border-radius: 2px; flex: 0 0 auto; }
.sv-meter-cell { display: grid; grid-template-columns: minmax(42px, 1fr) 44px; align-items: center; gap: 6px; }
.sv-meter-cell em { color: var(--muted); font-style: normal; text-align: right; }
.sv-inline-meter { height: 10px; min-width: 42px; border-radius: 5px; background: var(--surface-muted); overflow: hidden; }
.sv-inline-meter span { display: block; height: 100%; min-width: 1px; border-radius: 5px; }
.sv-empty-cell { color: var(--dim); text-align: center; padding: 16px 8px !important; }
.c-start { color: #5b8cff; }
.c-stop { color: #ff7675; }
.c-isr { color: #f5a623; }
.c-idle { color: var(--muted); }
@media (max-width: 1100px) {
  .sv-health-grid { grid-template-columns: repeat(3, minmax(118px, 1fr)); }
  .sv-bottom-grid { grid-template-columns: 1fr; }
  .sv-replay-bar { flex-wrap: wrap; }
  .sv-replay-bar progress { flex-basis: 160px; }
}
</style>
