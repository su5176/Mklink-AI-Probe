<template>
  <div class="sv-tab">
    <input
      ref="fileInput"
      class="sv-file-input"
      type="file"
      accept=".jsonl,application/x-ndjson,application/json"
      @change="onImportFileChange"
    >
    <div v-if="!deviceConnected && !offlineMode" class="sv-toolbar sv-offline-toolbar">
      <button class="btn-clear sv-tool-btn" @click="triggerImport">导入JSONL</button>
    </div>
    <div v-if="!deviceConnected && !offlineMode" class="alert alert-warn">请先连接设备。</div>
    <template v-if="deviceConnected || offlineMode">
      <div class="sv-toolbar">
        <ControlToolbar
          v-if="!offlineMode"
          :state="dash.state.value"
          :error="dash.error.value"
          :device-connected="deviceConnected"
          @start="onStart"
          @pause="dash.pause()"
          @resume="dash.resume()"
          @stop="onStop"
        />
        <button v-else class="btn-clear sv-mode-btn" @click="returnToLive">实时</button>
        <button class="btn-clear sv-tool-btn" @click="triggerImport">导入JSONL</button>
        <button class="btn-clear sv-tool-btn" :disabled="!currentJsonlPath" @click="exportLog(currentJsonlPath)">导出JSONL</button>
        <button class="btn-clear sv-tool-btn" :disabled="!currentSummaryPath" @click="exportLog(currentSummaryPath)">导出摘要</button>
        <label class="sv-window">
          窗口
          <select v-model.number="windowUs">
            <option :value="500_000">0.5s</option>
            <option :value="1_000_000">1s</option>
            <option :value="2_000_000">2s</option>
            <option :value="5_000_000">5s</option>
          </select>
        </label>
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
          <span class="sv-section-subtitle">最近 {{ eventRows.length }} 条</span>
          <span class="sv-section-actions">
            <button v-if="eventList.length > 0" class="btn-clear" @click="clearAll">清除</button>
            <button class="btn-clear" @click="showEventStream = !showEventStream">
              {{ showEventStream ? '折叠' : '展开' }}
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
                <td colspan="6" class="sv-empty-cell">等待 SystemView 事件。</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 甘特时间轴（交互式 canvas：Ctrl/Shift+滚轮缩放 · 拖拽平移 · hover · 图例隐藏 · 可见CPU%） -->
      <div class="sv-section sv-gantt-section">
        <div class="sv-section-title">
          <span>Timeline</span>
          <span class="sv-section-subtitle">Ctrl/Shift+滚轮缩放 · 拖拽平移 · hover 详情 · 点图例隐藏</span>
          <button class="btn-clear" @click="tlReset">全览</button>
        </div>
        <div class="sv-legend" ref="tlLegend"></div>
        <div class="sv-canvas-wrap"><canvas ref="tlCanvas"></canvas></div>
        <div class="sv-tip" ref="tlTip"></div>
        <div class="sv-vcpu-title">可见窗口内 CPU 占用</div>
        <div class="sv-vcpu" ref="tlVcpu"></div>
      </div>

      <div class="sv-bottom-grid">
        <div class="sv-section sv-runtime-section">
          <div class="sv-section-title">
            <span>Runtime</span>
            <span class="sv-section-subtitle">单次运行片段分布</span>
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
                  <td colspan="8" class="sv-empty-cell">还没有运行片段。</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="sv-section sv-context-section">
          <div class="sv-section-title">
            <span>Context</span>
            <span class="sv-section-subtitle">任务活动概览</span>
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
                  <td colspan="6" class="sv-empty-cell">还没有任务上下文。</td>
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
import { useDashboard } from '../../composables/useDashboard'
import { useEventSource } from '../../composables/useEventSource'
import { useResourceStatus } from '../../composables/useResourceStatus'
import { SvTimeline } from '../../lib/svTimeline'
import { appendManyToLast } from '../../lib/boundedBuffer'
import { takeNewStreamPoints } from '../../lib/streamCursor'
import { ingestSystemViewIntervals, type SystemViewIntervalState } from '../../lib/systemViewIntervals'
import { buildSystemViewEventRows, computeContextRows, computeRuntimeRows } from '../../lib/systemViewMetrics'
import { appendAndTrimEventsByTime, appendAndTrimRanges, filterRangesByWindow } from '../../lib/systemViewTimeBuffer'
import { formatScheduleCount } from '../../lib/systemViewLabels'
import { importSystemViewJsonl } from '../../lib/systemViewImport'
import ControlToolbar from './ControlToolbar.vue'

const props = defineProps<{ deviceConnected: boolean }>()

const dash = useDashboard('systemview')
const { data, connect, disconnect } = useEventSource('/api/dash/systemview/stream', {
  passthroughEvents: ['status', 'batch'],
})
const { checkConflict } = useResourceStatus()

// ---- 状态 ----
interface TaskStat { id: number; name: string; color: string; runUs: number; switches: number; prio?: number }
interface TaskInterval { taskId: number; start: number; end: number; startTk?: number; endTk?: number }
interface SystemViewLogItem { path: string; summary_path?: string }

const PALETTE = ['#5b8cff', '#21c7a8', '#f5a623', '#e056fd', '#ff7675', '#fdcb6e',
                 '#00cec9', '#a29bfe', '#55efc4', '#fab1a0', '#74b9ff', '#fd79a8']
const fileInput = ref<HTMLInputElement | null>(null)
const eventList = shallowRef<any[]>([])
let analysisEvents: any[] = []
const analysisBufferCount = ref(0)
const taskStats = reactive<Record<number, TaskStat>>({})
const intervals = shallowRef<TaskInterval[]>([])
let intervalState: SystemViewIntervalState = { currentTaskId: null, currentStart: null }
const idleUs = ref(0)
let firstT = 0
let lastT = 0
const meta = reactive({
  synced: false,
  dropped: 0,
  sessionDropped: 0,
  cpuFreq: 0,
  cpuFreqSource: '',
  taskNames: {} as Record<number, string>,
  recordingPath: '',
  recordingSummaryPath: '',
  recordingError: '',
})
let lastStreamSeq = 0
const totalEventCount = ref(0)
const windowUs = ref(2_000_000)
const showEventStream = ref(true)
const offlineMode = ref(false)
const offlineFileName = ref('')
const importStatus = ref('')
const importError = ref(false)
const latestLog = ref<SystemViewLogItem | null>(null)
let importAbort: AbortController | null = null

// ---- 交互式 canvas 时间轴 ----
const tlCanvas = ref<HTMLCanvasElement | null>(null)
const tlTip = ref<HTMLDivElement | null>(null)
const tlLegend = ref<HTMLDivElement | null>(null)
const tlVcpu = ref<HTMLDivElement | null>(null)
let tlInstance: SvTimeline | null = null

function tlGetIntervals() {
  const visible = meta.cpuFreq
    ? filterRangesByWindow(intervals.value, lastT, windowUs.value)
    : intervals.value
  return visible.map(it => ({
    tid: it.taskId,
    name: taskStats[it.taskId]?.name || meta.taskNames[it.taskId] || ('0x' + (it.taskId >>> 0).toString(16).toUpperCase()),
    start: it.start, end: it.end, startTk: it.startTk, endTk: it.endTk,
  }))
}
function tlReset() { tlInstance?.reset() }

onMounted(() => {
  refreshLogList()
  if (tlCanvas.value && tlTip.value && tlLegend.value && tlVcpu.value) {
    tlInstance = new SvTimeline(
      { canvas: tlCanvas.value, tooltip: tlTip.value, legend: tlLegend.value, vcpu: tlVcpu.value },
      {
        intervals: tlGetIntervals(),
        unit: meta.cpuFreq ? 'us' : 'tk',
        tickHz: meta.cpuFreq || undefined,
        follow: true,
        windowSize: windowUs.value,
      },
    )
  }
})
onUnmounted(() => {
  abortImport()
  tlInstance?.destroy()
  tlInstance = null
})

// intervals 变化时喂给时间轴（rAF 节流：高频事件下 intervals 频繁 push，
// 每次 setData+canvas redraw 会卡死浏览器；标记 dirty，每帧最多 redraw 一次）
let tlDirty = false
let tlRaf = 0
function tlFlush() { tlRaf = 0; if (tlInstance && tlDirty) { tlDirty = false; tlInstance.setData(tlGetIntervals()) } }
function scheduleTimelineFlush() {
  tlDirty = true
  if (!tlRaf) tlRaf = requestAnimationFrame(tlFlush)
}
watch(intervals, scheduleTimelineFlush)
watch(windowUs, () => {
  tlInstance?.setWindowSize(windowUs.value)
  scheduleTimelineFlush()
})
// cpuFreq 变了切换单位（重建）
watch(() => meta.cpuFreq, () => {
  if (tlCanvas.value && tlTip.value && tlLegend.value && tlVcpu.value) {
    tlInstance?.destroy()
    tlInstance = new SvTimeline(
      { canvas: tlCanvas.value, tooltip: tlTip.value, legend: tlLegend.value, vcpu: tlVcpu.value },
      {
        intervals: tlGetIntervals(),
        unit: meta.cpuFreq ? 'us' : 'tk',
        tickHz: meta.cpuFreq || undefined,
        follow: true,
        windowSize: windowUs.value,
      },
    )
  }
})

const MAX_EVENTS = 800
const ANALYSIS_EVENTS = 100_000
const MAX_INTERVALS = 50_000
const ANALYSIS_BUFFER_US = 60_000_000

function taskNameFromMeta(id: number): string {
  return meta.taskNames[id] || meta.taskNames[String(id) as any] || ''
}

function applyTaskNames(names: Record<number, string>) {
  meta.taskNames = names
  for (const [idText, name] of Object.entries(names)) {
    const id = Number(idText)
    if (Number.isFinite(id) && taskStats[id] && name) {
      taskStats[id].name = name
    }
  }
}

function colorFor(id: number): string {
  if (taskStats[id]) return taskStats[id].color
  const idx = Object.keys(taskStats).length % PALETTE.length
  return PALETTE[idx]
}

function ensureTask(id: number, name?: string): TaskStat {
  if (!taskStats[id]) {
    taskStats[id] = { id, name: name || taskNameFromMeta(id), color: colorFor(id), runUs: 0, switches: 0 }
  }
  const resolvedName = name || taskNameFromMeta(id)
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

watch(data, (nw) => {
  if (offlineMode.value) return
  const fresh = takeNewStreamPoints(nw as any[], lastStreamSeq)
  for (const dp of fresh.points as any[]) {
    const evt = dp.event || dp._event
    if (dp.synced !== undefined) meta.synced = !!dp.synced
    if (dp.dropped_bytes !== undefined) meta.sessionDropped = dp.dropped_bytes + (dp.dropped_packets || 0)
    if (dp.runtime_dropped_bytes !== undefined || dp.dropped_bytes !== undefined) {
      meta.dropped = (dp.runtime_dropped_bytes ?? dp.dropped_bytes ?? 0) + (dp.dropped_packets || 0)
    }
    if (dp.cpu_freq !== undefined) meta.cpuFreq = dp.cpu_freq
    if (dp.cpu_freq_source !== undefined) meta.cpuFreqSource = dp.cpu_freq_source || ''
    if (dp.recording_path !== undefined) meta.recordingPath = dp.recording_path || meta.recordingPath
    if (dp.recording_summary_path !== undefined) meta.recordingSummaryPath = dp.recording_summary_path || meta.recordingSummaryPath
    if (dp.recording_error !== undefined) meta.recordingError = dp.recording_error || ''
    const backendEvents = Number(dp.stats?.events)
    if (Number.isFinite(backendEvents)) totalEventCount.value = Math.max(totalEventCount.value, backendEvents)
    if (dp.task_names) applyTaskNames(dp.task_names)
    if (evt === 'status' || evt === 'history') {
      continue
    }
    if (evt === 'batch') { ingestEvents(dp.events || [], !Number.isFinite(backendEvents)); continue }
    if (evt === 'data' || !evt) ingestEvents([dp])
  }
  lastStreamSeq = fresh.nextSeq
})

// ---- 计算属性 ----
const eventCount = computed(() => totalEventCount.value)
const taskCount = computed(() => Object.keys(taskStats).length)

const tableEvents = computed(() => eventList.value.slice(-120))
const eventRows = computed(() => buildSystemViewEventRows(tableEvents.value, {
  firstIndex: Math.max(1, totalEventCount.value - tableEvents.value.length + 1),
  formatTime: value => fmtTime(value),
}))
const runtimeRows = computed(() => computeRuntimeRows(Object.values(taskStats), intervals.value))
const contextRows = computed(() => computeContextRows(Object.values(taskStats)))
const currentJsonlPath = computed(() => meta.recordingPath || latestLog.value?.path || '')
const currentSummaryPath = computed(() => meta.recordingSummaryPath || latestLog.value?.summary_path || '')

// ---- 辅助 ----
function clamp(v: number) { return Math.max(0, Math.min(100, v)) }
function hexId(id: number) { return '0x' + (id >>> 0).toString(16).toUpperCase() }
function fmtCpuFreq(freq: number) {
  return freq >= 1_000_000 ? (freq / 1_000_000).toFixed(0) + 'MHz' : freq.toLocaleString() + 'Hz'
}
function fmtTime(t: any) {
  if (typeof t === 'number' && meta.cpuFreq) return (t / 1_000_000).toFixed(6) + 's'
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
  eventList.value = []
  analysisEvents = []
  analysisBufferCount.value = 0
  intervals.value = []
  Object.keys(taskStats).forEach(k => delete taskStats[Number(k)])
  intervalState = { currentTaskId: null, currentStart: null }
  totalEventCount.value = 0
  idleUs.value = 0; firstT = 0; lastT = 0; lastStreamSeq = 0
  meta.synced = false
  meta.dropped = 0
  meta.sessionDropped = 0
  meta.cpuFreq = 0
  meta.cpuFreqSource = ''
  meta.taskNames = {}
  meta.recordingPath = ''
  meta.recordingSummaryPath = ''
  meta.recordingError = ''
}

async function refreshLogList() {
  try {
    const res = await fetch('/api/dash/systemview/logs')
    if (!res.ok) return
    const body = await res.json()
    latestLog.value = Array.isArray(body.logs) ? body.logs[0] || null : null
  } catch {
    latestLog.value = null
  }
}

function exportLog(path: string) {
  if (!path) return
  window.open(`/api/dash/systemview/logs/download?path=${encodeURIComponent(path)}`, '_blank')
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
  abortImport()
  const controller = new AbortController()
  importAbort = controller
  disconnect()
  if (dash.state.value !== 'idle') {
    await dash.stop()
  }
  clearAll()
  offlineMode.value = true
  offlineFileName.value = file.name
  importStatus.value = '导入中'
  importError.value = false
  meta.synced = true

  try {
    const result = await importSystemViewJsonl({
      stream: file.stream(),
      batchSize: 1000,
      signal: controller.signal,
      onSession: record => applyImportedMeta(record),
      onSummary: record => applyImportedMeta(record),
      onBatch: events => {
        ingestEvents(events, true)
        importStatus.value = `导入中 ${totalEventCount.value.toLocaleString()}`
      },
    })
    if (importAbort !== controller) return
    const suffix = result.parseErrors || result.skipped
      ? `，跳过 ${result.skipped.toLocaleString()}，错误 ${result.parseErrors.toLocaleString()}`
      : ''
    importStatus.value = `已导入 ${result.events.toLocaleString()}${suffix}`
    importError.value = result.parseErrors > 0
  } catch (e) {
    if (importAbort !== controller) return
    importError.value = !isAbortError(e)
    importStatus.value = isAbortError(e)
      ? '已取消'
      : `导入失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    if (importAbort === controller) importAbort = null
    scheduleTimelineFlush()
  }
}

function applyImportedMeta(record: Record<string, unknown>) {
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
    applyTaskNames(record.task_names as Record<number, string>)
  }
}

function abortImport() {
  if (importAbort) {
    importAbort.abort()
    importAbort = null
  }
}

function returnToLive() {
  abortImport()
  offlineMode.value = false
  offlineFileName.value = ''
  importStatus.value = ''
  importError.value = false
  clearAll()
  refreshLogList()
}

function isAbortError(value: unknown): boolean {
  return value instanceof Error && /aborted/i.test(value.message)
}

async function onStart() {
  abortImport()
  offlineMode.value = false
  offlineFileName.value = ''
  importStatus.value = ''
  importError.value = false
  latestLog.value = null
  const conflicts = await checkConflict('systemview')
  if (conflicts.length > 0) {
    const names = conflicts.map(c => c).join('、')
    if (!confirm(`启动 SystemView 将停止当前运行的 ${names} 会话。确认？`)) return
  }
  clearAll()
  await dash.start()
  setTimeout(() => connect(), 500)
}
async function onStop() {
  disconnect()
  await dash.stop()
  await refreshLogList()
}
</script>

<style scoped>
.sv-tab { display: flex; flex-direction: column; height: 100%; gap: 8px; }
.alert-warn { color: var(--warn); padding: 8px; border: 1px solid var(--warn); border-radius: 4px; }
.sv-toolbar { display: flex; align-items: center; gap: 12px; padding: 6px 0; flex-wrap: wrap; }
.sv-offline-toolbar { padding-bottom: 0; }
.sv-file-input { display: none; }
.sv-stat { font-size: 12px; color: var(--muted); }
.sv-stat b { color: var(--fg); }
.sv-stat.warn { color: var(--warn); }
.sv-window { font-size: 12px; color: var(--muted); margin-left: auto; display: flex; align-items: center; gap: 4px; }
.sv-window select { background: #fbfaf5; color: var(--fg); border: 1px solid #d8d2c3; border-radius: 4px; padding: 2px 4px; }
.sv-section { border: 1px solid var(--border); border-radius: var(--radius); padding: 8px; }
.sv-section-title { font-size: 12px; color: var(--muted); margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
.sv-section-title > span:first-child { color: var(--fg); font-weight: 650; }
.sv-section-subtitle { color: var(--dim); font-size: 11px; font-weight: 400; }
.btn-clear { margin-left: auto; background: none; border: 1px solid var(--border); border-radius: 4px; color: var(--muted); font-size: 11px; padding: 1px 8px; cursor: pointer; }
.btn-clear:disabled { opacity: .45; cursor: not-allowed; }
.sv-tool-btn,
.sv-mode-btn { margin-left: 0; }
.sv-section-actions { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }
.sv-section-actions .btn-clear { margin-left: 0; }
.sv-empty { color: var(--dim); font-size: 12px; padding: 12px; text-align: center; }

.sv-health-grid { display: grid; grid-template-columns: repeat(6, minmax(118px, 1fr)); gap: 8px; }
.sv-health-card { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); background: #fffdf8; padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; }
.sv-health-card span { color: var(--dim); font-size: 11px; }
.sv-health-card b { color: var(--fg); font-size: 15px; font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sv-health-card.warn { border-color: rgba(224, 86, 253, .45); background: #fff7fb; }
.sv-health-card.warn span,
.sv-health-card.warn b { color: var(--warn); }
.sv-health-wide { grid-column: span 2; }

/* 甘特 */
.sv-gantt-section { flex: 0 0 auto; min-height: 0; display: flex; flex-direction: column; }
.sv-legend { display: flex; gap: 6px; flex-wrap: wrap; align-content: flex-start; height: 28px; overflow-y: auto; scrollbar-gutter: stable; margin: 4px 0; }
.sv-legend :deep(.sv-lg) { display: inline-flex; align-items: center; gap: 4px; background: #fbfaf5; color: #374151; border: 1px solid #ddd8ca; border-radius: 12px; padding: 2px 9px; font-size: 11px; cursor: pointer; user-select: none; box-shadow: inset 0 -1px 0 rgba(0,0,0,.03); }
.sv-legend :deep(.sv-lg i) { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.sv-legend :deep(.sv-lg em) { color: #6b7280; font-style: normal; }
.sv-legend :deep(.sv-lg-off) { opacity: .4; text-decoration: line-through; }
.sv-canvas-wrap { position: relative; background: #fbfaf5; border: 1px solid var(--border); border-radius: var(--radius); overflow: visible; }
.sv-canvas-wrap :deep(canvas) { display: block; width: 100%; cursor: crosshair; }
.sv-tip { position: fixed; display: none; background: #1c2128; border: 1px solid #444c56; border-radius: 6px; padding: 6px 10px; font-size: 11px; color: #f0f6fc; pointer-events: none; z-index: 99; font-family: var(--font-mono, monospace); white-space: nowrap; }
.sv-vcpu-title { font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
.sv-vcpu { display: flex; flex-direction: column; gap: 1px; height: 96px; overflow-y: auto; scrollbar-gutter: stable; padding-right: 2px; }
.sv-vcpu :deep(.sv-vcpu-row) { display: flex; align-items: center; gap: 8px; font-size: 11px; margin: 1px 0; }
.sv-vcpu :deep(.sv-vcpu-n) { width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }
.sv-vcpu :deep(.sv-vcpu-bg) { flex: 1; height: 11px; background: #e7e2d6; border-radius: 6px; overflow: hidden; }
.sv-vcpu :deep(.sv-vcpu-bar) { height: 100%; border-radius: 6px; }

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
.sv-table-wrap { overflow: auto; border: 1px solid #e7e2d6; border-radius: 4px; background: #fbfaf5; }
.sv-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; font-variant-numeric: tabular-nums; }
.sv-table th { position: sticky; top: 0; z-index: 1; background: #f4f1e8; color: #605a50; font-weight: 650; text-align: left; border-bottom: 1px solid #d8d2c3; padding: 5px 6px; white-space: nowrap; }
.sv-table td { border-bottom: 1px solid #ebe5d9; color: #374151; padding: 4px 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sv-table tbody tr:nth-child(even) td { background: #fffdf8; }
.sv-table tbody tr:hover td { background: #eef6ff; }
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
.sv-inline-meter { height: 10px; min-width: 42px; border-radius: 5px; background: #e7e2d6; overflow: hidden; }
.sv-inline-meter span { display: block; height: 100%; min-width: 1px; border-radius: 5px; }
.sv-empty-cell { color: var(--dim); text-align: center; padding: 16px 8px !important; }
.c-start { color: #5b8cff; }
.c-stop { color: #ff7675; }
.c-isr { color: #f5a623; }
.c-idle { color: #555; }
@media (max-width: 1100px) {
  .sv-health-grid { grid-template-columns: repeat(3, minmax(118px, 1fr)); }
  .sv-bottom-grid { grid-template-columns: 1fr; }
}
</style>
