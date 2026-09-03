<template>
  <div
    ref="container"
    class="waveform-viewer"
    :class="{ 'superwatch-desktop': props.mode === 'SuperWatch' }"
  ></div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useBinaryStream } from '../../composables/useBinaryStream'
import { RenderScheduler } from '../../lib/stream/renderScheduler'
import type { WorkerOutput } from '../../workers/streamDecoder.worker'
import '../../assets/rtt_viewer.css'
import i18nUrl from '../../assets/rtt_i18n.js?url'
import viewerUrl from '../../assets/rtt_viewer.js?url'
import { language } from '../../composables/useLanguage'
import { API_BASE } from '../../lib/runtimeEndpoint'
import { saveTextFile } from '../../lib/downloadTextFile'

const props = defineProps<{
  mode: 'SuperWatch' | 'VOFA'
  deviceConnected: boolean
  hiddenChannels?: ReadonlySet<string>
  arraySnapshotPath?: string | null
}>()

const emit = defineEmits<{
  'latest-values': [values: Record<string, number>]
}>()

const container = ref<HTMLDivElement>()
const binary = useBinaryStream(
  props.mode === 'VOFA' ? 'vofa' : 'superwatch',
  { capacity: 200000, channelCount: 1 },
)
let vofaChannels: Array<Record<string, unknown>> = []
let vofaChannelSignature: string | null = null
let pendingBatch: Extract<WorkerOutput, { type: 'waveform-batch' }> | null = null
let visibleRequestId = 0
let historyRequestId = 0
let visibleRequestInFlight: number | null = null
let visibleRangePending = false
let visibleRangePendingInteractive = false
const interactiveVisibleRequests = new Set<number>()
let previousTransportPhase = 'stopped'
let statusPollTimer: ReturnType<typeof setTimeout> | null = null
let statusPollGeneration = 0
let latestVofaStatus: Record<string, unknown> | null = null
let arraySnapshotTimer: ReturnType<typeof setTimeout> | null = null
let arraySnapshotGeneration = 0
let disposed = false
function requestLatestVisibleRange(interactive = false): void {
  if (visibleRequestInFlight !== null) {
    visibleRangePending = true
    visibleRangePendingInteractive ||= interactive
    return
  }
  const viewer = (window as any).__waveformViewers?.[props.mode]
  const range = viewer?.getBinaryVisibleRange?.()
  if (!range) return
  const { start, end, pixelWidth } = range
  if (![start, end, pixelWidth].every(Number.isFinite) || pixelWidth < 1) return
  visibleRangePending = false
  visibleRangePendingInteractive = false
  visibleRequestInFlight = ++visibleRequestId
  if (interactive) interactiveVisibleRequests.add(visibleRequestInFlight)
  binary.requestVisibleRange(visibleRequestInFlight, start, end, pixelWidth)
}

function resetVisibleRangeRequests(): void {
  visibleRequestId++
  visibleRequestInFlight = null
  visibleRangePending = false
  visibleRangePendingInteractive = false
  interactiveVisibleRequests.clear()
}

const vofaScheduler = new RenderScheduler(() => requestLatestVisibleRange(false))

function attachSuperwatchRequesters(viewer: any): void {
  if (props.mode !== 'SuperWatch' || !viewer) return
  viewer.setBinaryHistoryRequester?.(() => {
    binary.requestHistorySnapshot?.(++historyRequestId)
  })
  viewer.setBinaryDetailRequester?.((enabled: boolean) => {
    binary.setWaveformDetail?.(enabled)
  })
  viewer.setBinaryVisibleRangeRequester?.(() => requestLatestVisibleRange(true))
}

function channelSignature(channels: readonly Record<string, unknown>[]): string {
  return JSON.stringify(channels.map((channel, index) => [
    channel.name ?? channel.addr ?? channel.address ?? index,
    channel.type ?? 'float', channel.size ?? 4,
    channel.addr ?? channel.address ?? null, channel.unit ?? '',
  ]))
}

function applyVofaChannels(channels: readonly Record<string, unknown>[]): void {
  if (disposed) return
  const signature = channelSignature(channels)
  if (signature === vofaChannelSignature) return
  vofaChannelSignature = signature
  vofaChannels = channels.map(channel => ({ ...channel }))
  resetVisibleRangeRequests()
  pendingBatch = null
  if (props.mode === 'VOFA') binary.configure(Math.max(1, channels.length))
  const viewer = (window as any).__waveformViewers?.[props.mode]
  viewer?.configureBinaryChannels?.(vofaChannels)
  applyHiddenChannels()
}

function applyHiddenChannels(): void {
  const names = [...(props.hiddenChannels ?? [])].sort()
  ;(window as any).__waveformViewers?.[props.mode]?.setHiddenChannels?.(names)
}

function stopArraySnapshotPolling(): void {
  arraySnapshotGeneration += 1
  if (arraySnapshotTimer !== null) {
    clearTimeout(arraySnapshotTimer)
    arraySnapshotTimer = null
  }
}

async function pollArraySnapshot(generation: number): Promise<void> {
  if (disposed || props.mode !== 'SuperWatch' || !props.arraySnapshotPath || !props.deviceConnected) return
  try {
    const response = await fetch(`${API_BASE}/api/dash/superwatch/array-snapshot`)
    const payload = await response.json().catch(() => ({}))
    if (response.ok && payload?.snapshot?.name === props.arraySnapshotPath) {
      ;(window as any).__waveformViewers?.SuperWatch?.setArraySnapshot?.(payload.snapshot)
    } else {
      ;(window as any).__waveformViewers?.SuperWatch?.setArraySnapshot?.(null)
    }
  } catch {
    // The binary stream remains independent when the optional snapshot poll fails.
  }
  if (!disposed && generation === arraySnapshotGeneration && props.arraySnapshotPath) {
    arraySnapshotTimer = setTimeout(() => {
      arraySnapshotTimer = null
      void pollArraySnapshot(generation)
    }, 50)
  }
}

function startArraySnapshotPolling(): void {
  stopArraySnapshotPolling()
  if (props.mode !== 'SuperWatch' || !props.arraySnapshotPath || !props.deviceConnected) {
    ;(window as any).__waveformViewers?.SuperWatch?.setArraySnapshot?.(null)
    return
  }
  const generation = arraySnapshotGeneration
  void pollArraySnapshot(generation)
}

function onVofaChannels(event: Event): void {
  const channels = (event as CustomEvent<unknown>).detail
  if (!Array.isArray(channels)) return
  applyVofaChannels(channels as Array<Record<string, unknown>>)
}

function resetVofaSession(): void {
  if (disposed) return
  resetVisibleRangeRequests()
  pendingBatch = null
  binary.reset()
  ;(window as any).__waveformViewers?.[props.mode]?.resetBinaryStream?.()
}

function stopVofaStatusPolling(): void {
  statusPollGeneration++
  if (statusPollTimer !== null) {
    clearTimeout(statusPollTimer)
    statusPollTimer = null
  }
}

async function pollVofaStatus(generation: number, startTransport: boolean): Promise<void> {
  let status: Record<string, unknown> | null = null
  try {
    const response = await fetch(`${API_BASE}/api/dash/${props.mode === 'VOFA' ? 'vofa' : 'superwatch'}/status`)
    status = response.ok ? await response.json() : null
  } catch { /* retry on the bounded cadence below */ }
  if (disposed || generation !== statusPollGeneration) return
  if (status) {
    latestVofaStatus = status
    if (startTransport) {
      const channels = props.mode === 'VOFA' ? status.channels : status.items
      applyVofaChannels(Array.isArray(channels) ? channels : [])
    }
    ;(window as any).__waveformViewers?.[props.mode]?.updateAcquisitionStatus?.(status)
  }
  if (startTransport) binary.start()
  statusPollTimer = setTimeout(() => {
    statusPollTimer = null
    void pollVofaStatus(generation, false)
  }, 1_000)
}

function startVofaStatusPolling(startTransport: boolean): void {
  if (disposed) return
  stopVofaStatusPolling()
  const generation = statusPollGeneration
  void pollVofaStatus(generation, startTransport)
}

function onVofaStreamState(event: Event): void {
  const state = (event as CustomEvent<unknown>).detail
  if (state === 'running') {
    if (props.mode === 'SuperWatch') binary.stop()
    resetVofaSession()
    if (props.mode === 'SuperWatch') binary.start()
    startVofaStatusPolling(false)
  } else if (state === 'stopped') {
    pendingBatch = null
    binary.stop()
    stopVofaStatusPolling()
  }
}

watch(() => binary.waveformBatch.value, batch => {
  if (!batch) return
  pendingBatch = batch
  if (
    props.mode === 'SuperWatch'
    && !binary.waveformSummary
    && batch.itemCount > 0
    && batch.channelCount === vofaChannels.length
  ) {
    const values = new Float32Array(batch.values)
    const offset = (batch.itemCount - 1) * batch.channelCount
    const latest: Record<string, number> = {}
    for (let channel = 0; channel < batch.channelCount; channel += 1) {
      const name = String(vofaChannels[channel]?.name ?? '')
      const value = values[offset + channel]
      if (name && Number.isFinite(value)) latest[name] = value
    }
    emit('latest-values', latest)
  }
  const viewer = (window as any).__waveformViewers?.[props.mode]
  if (viewer?.acceptBinaryBatch) {
    viewer.acceptBinaryBatch(batch, vofaChannels)
    pendingBatch = null
  }
  if (props.mode !== 'SuperWatch') {
    vofaScheduler?.recordCollection(batch.itemCount)
    vofaScheduler?.invalidate('data')
  }
})

watch(() => binary.waveformSummary?.value, summary => {
  if (!summary || props.mode !== 'SuperWatch') return
  const values = new Float32Array(summary.latestValues)
  if (summary.channelCount === vofaChannels.length && values.length === summary.channelCount) {
    const latest: Record<string, number> = {}
    for (let channel = 0; channel < summary.channelCount; channel += 1) {
      const name = String(vofaChannels[channel]?.name ?? '')
      if (name && Number.isFinite(values[channel])) latest[name] = values[channel]
    }
    emit('latest-values', latest)
  }
  ;(window as any).__waveformViewers?.SuperWatch?.acceptBinarySummary?.(summary, vofaChannels)
  vofaScheduler?.recordCollection(summary.collectedItemCount)
  vofaScheduler?.invalidate('data')
})

watch(() => binary.historySnapshot?.value, snapshot => {
  if (!snapshot || props.mode !== 'SuperWatch') return
  ;(window as any).__waveformViewers?.SuperWatch?.exportBinaryHistorySnapshot?.(snapshot)
})

watch(() => binary.envelope.value, envelope => {
  if (!envelope || envelope.requestId !== visibleRequestInFlight) return
  visibleRequestInFlight = null
  const renderWhilePaused = interactiveVisibleRequests.delete(envelope.requestId)
  ;(window as any).__waveformViewers?.[props.mode]?.renderBinaryEnvelope?.(
    envelope, renderWhilePaused,
  )
  if (visibleRangePending) {
    const interactive = visibleRangePendingInteractive
    requestLatestVisibleRange(interactive)
  }
})

watch(() => binary.superwatchMetadata.value, metadata => {
  if (!metadata || props.mode !== 'SuperWatch') return
  applyVofaChannels(metadata.channels)
})

watch([
  () => binary.state.value,
  () => binary.telemetry.value,
  () => binary.error.value,
], ([state, telemetry, error]) => {
  if (!state) return
  if (state.phase === 'reconnecting' && previousTransportPhase !== 'reconnecting') {
    resetVofaSession()
  }
  previousTransportPhase = state.phase
  ;(window as any).__waveformViewers?.[props.mode]?.updateBinaryHealth?.({
    phase: state.phase,
    reconnectDelayMs: state.reconnectDelayMs,
    bufferedSamples: telemetry?.bufferedSamples ?? 0,
    transportDroppedBatches: telemetry?.transportDroppedBatches ?? 0,
    backendDroppedBatches: telemetry?.backendDroppedBatches ?? 0,
    backendDroppedItems: telemetry?.backendDroppedItems ?? 0,
    error: state.phase === 'connected' || state.phase === 'stopped' ? null : error,
  })
})

onMounted(() => {
  if (!container.value) return
  const el = container.value

  const saveViewerFile = (filename: string, text: string) => saveTextFile(filename, text)
  ;(window as any).__MKLINK_SAVE_FILE__ = saveViewerFile

  // 1. Inject HTML template
  el.innerHTML = buildTemplate(props.mode)

  // 2. Inject CONFIG + load scripts
  injectScripts(el, props.mode)
  {
    if (props.mode === 'VOFA') {
      window.addEventListener('mklink:vofa-channels', onVofaChannels)
    }
    window.addEventListener('mklink:vofa-stream-state', onVofaStreamState)
    vofaScheduler?.start()
    startVofaStatusPolling(true)
    startArraySnapshotPolling()
    const viewer = (window as any).__waveformViewers?.[props.mode]
    attachSuperwatchRequesters(viewer)
  }
})

watch(() => props.deviceConnected, (val) => {
  const viewers = (window as any).__waveformViewers
  if (viewers?.[props.mode]?.setDeviceConnected) viewers[props.mode].setDeviceConnected(val)
})

watch(() => props.hiddenChannels, applyHiddenChannels)
watch([() => props.arraySnapshotPath, () => props.deviceConnected], startArraySnapshotPolling)

watch(language, value => {
  ;(window as any).setLang?.(value)
})

onUnmounted(() => {
  disposed = true
  stopVofaStatusPolling()
  stopArraySnapshotPolling()
  binary.stop()
  vofaScheduler?.dispose()
  window.removeEventListener('mklink:vofa-channels', onVofaChannels)
  window.removeEventListener('mklink:vofa-stream-state', onVofaStreamState)
  // Close EventSource if running
  try {
    const viewers = (window as any).__waveformViewers
    viewers?.[props.mode]?.dispose?.()
    if (viewers) delete viewers[props.mode]
  } catch { /* ignore */ }
  // Clear DOM
  if (container.value) container.value.innerHTML = ''
  if ((window as any).__MKLINK_SAVE_FILE__) delete (window as any).__MKLINK_SAVE_FILE__
})

function buildTemplate(mode: string): string {
  const minPoints = mode === 'SuperWatch' ? 50000 : 2
  const maxPoints = mode === 'SuperWatch' ? 50000 : 10000
  const intervalValue = mode === 'SuperWatch' ? '0.001' : '0'
  const intervalMinimum = mode === 'SuperWatch' ? '0.00001' : '0'
  const intervalStep = mode === 'SuperWatch' ? '0.00001' : '0.001'
  return `
<header>
  <div class="header-status">
    <h1>MKLink ${mode}</h1>
    <span id="mode-badge" class="badge badge-mode">${mode}</span>
    <span id="conn-status" class="badge badge-ok" data-i18n="live">live</span>
    <span id="pts-count" class="badge badge-info">0 pts</span>
    <span id="sample-rate-badge" class="badge badge-info">-- Hz</span>
    <span id="transport-state-badge" class="badge badge-info">transport stopped</span>
    <span id="transport-health-badge" class="badge badge-info">transport 0 / backend 0/0 / buffer 0</span>
  </div>
  <div class="header-actions">
    <button id="btn-cursor-toggle" class="panel-btn" data-i18n-title="cursors_tip" data-i18n="cursors">Cursors</button>
    <button id="btn-cursor-mode" class="panel-btn" style="display:none;" data-i18n-title="cursor_mode_tip">Time</button>
    <button id="btn-save-project" class="panel-btn" data-i18n-title="save_project_tip" data-i18n="save">Save</button>
    <button id="btn-load-project" class="panel-btn" data-i18n-title="load_project_tip" data-i18n="load">Load</button>
    <button id="btn-thresholds" class="panel-btn" data-i18n-title="thresholds_tip" data-i18n="thresholds">Thresholds</button>
    <button id="btn-export-csv" class="panel-btn" data-i18n-title="export_csv_tip">CSV</button>
    <button id="btn-export-png" class="panel-btn" data-i18n-title="export_png_tip">PNG</button>
    <button id="btn-help" class="panel-btn" data-i18n-title="help_tip">?</button>
    <input id="project-load-input" class="hidden-file-input" type="file" accept="application/json,.json">
  </div>
</header>

<div id="control-toolbar">
  <button id="btn-start" class="ctrl-btn active" data-i18n="start">Start</button>
  <button id="btn-pause" class="ctrl-btn" data-i18n="pause">Pause</button>
  <button id="btn-stop" class="ctrl-btn danger" data-i18n="stop">Stop</button>
  <span id="collection-status-badge" class="status-running" data-i18n="running">Running</span>
  <div class="ctrl-sep"></div>
  <label data-i18n="buffer">Buffer</label>
  <input type="number" id="buffer-input" value="${maxPoints}" min="${minPoints}" max="1000000" step="10000">
  <span class="buffer-unit">pts/ch</span>
  <span id="buffer-memory-estimate" class="buffer-memory-estimate" data-i18n-title="buffer_memory_tip">~0 MB</span>
  <button id="btn-apply-buffer" class="ctrl-btn" data-i18n="apply">Apply</button>
  <div class="ctrl-sep"></div>
  <div id="interval-group">
    <label data-i18n="interval">Interval</label>
    <input type="number" id="interval-input" value="${intervalValue}" step="${intervalStep}" min="${intervalMinimum}" max="60">
    <span class="interval-unit">s</span>
    <button id="btn-apply-interval" class="ctrl-btn" data-i18n="apply">Apply</button>
  </div>
</div>

<div id="trigger-toolbar">
  <button id="trigger-enable-btn" data-i18n="trigger">Trigger</button>
  <span id="trigger-state-badge" class="trigger-state-idle" data-i18n="idle">Idle</span>
  <div class="trigger-sep"></div>
  <label data-i18n="source">Source</label>
  <select id="trigger-source"><option value="">--</option></select>
  <div class="trigger-sep"></div>
  <label data-i18n="edge">Edge</label>
  <select id="trigger-edge">
    <option value="rising" data-i18n="rising">Rising</option>
    <option value="falling" data-i18n="falling">Falling</option>
    <option value="both" data-i18n="both">Both</option>
  </select>
  <div class="trigger-sep"></div>
  <label data-i18n="level">Level</label>
  <input type="number" id="trigger-level" value="0" step="0.1">
  <div class="trigger-sep"></div>
  <label data-i18n="mode">Mode</label>
  <select id="trigger-mode">
    <option value="auto" data-i18n="auto">Auto</option>
    <option value="normal" data-i18n="normal">Normal</option>
    <option value="single" data-i18n="single">Single</option>
  </select>
  <div class="trigger-sep"></div>
  <label data-i18n="pretrig">Pre-trig</label>
  <input type="number" id="trigger-pretrig" value="1000" min="10" max="50000" step="100">
  <div class="trigger-sep"></div>
  <button id="trigger-force-btn" data-i18n="force_trigger">Force Trigger</button>
</div>

<div id="var-selector"></div>

<main id="debug-main">
  <section id="chart-watch-wrap">
    <div id="enum-tooltip"></div>
    <div id="chart-wrap">
      <canvas id="chart"></canvas>
      <button
        id="chart-legend-toggle"
        type="button"
        aria-expanded="true"
        aria-controls="chart-legend"
        data-i18n-title="hide_channel_legend_tip"
        title="隐藏通道列表"
      ><span aria-hidden="true">&#9776;</span></button>
      <div id="chart-legend" class="is-visible" aria-label="Channel layout" aria-hidden="false"></div>
      <div id="split-panel-control" class="split-panel-control" hidden>
        <span class="split-panel-label" data-i18n="split_panel">独立通道</span>
        <span id="split-panel-name" class="split-panel-name"></span>
        <button
          id="split-panel-merge"
          type="button"
          data-i18n="merge_channel"
          data-i18n-title="merge_channel_tip"
          title="将此通道合并回主面板"
        >合并</button>
      </div>
      <div id="y-axis-hit" class="axis-hit-region axis-hit-y" data-i18n-title="y_axis_tip" title="纵轴：滚轮缩放；按住鼠标左键拖动；双击恢复自动范围"></div>
      <div id="x-axis-hit" class="axis-hit-region axis-hit-x" data-i18n-title="x_axis_tip" title="横轴：滚轮缩放；按住鼠标左键拖动；双击恢复默认视图"></div>
      <div id="tooltip"></div>
      <div id="cursor-a" class="cursor-line" style="display:none;"></div>
      <div id="cursor-b" class="cursor-line" style="display:none;"></div>
      <div id="cursor-measure-panel" style="display:none;"></div>
    </div>
    <div id="watch-resizer"></div>
    <div id="watch-panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="panel-dot"></span>
          <span data-i18n="watch">监视</span>
        </div>
        <div class="panel-actions">
          <span id="watch-count" class="panel-count">0 ch</span>
          <button id="watch-columns-btn" class="panel-btn" data-i18n-title="columns_tip" data-i18n="columns">列</button>
          <button id="watch-collapse" class="panel-btn panel-btn-close" data-i18n-title="collapse_watch" title="折叠监视面板">&#x2715;</button>
        </div>
      </div>
      <div id="watch-columns-menu" class="columns-menu" aria-hidden="true"></div>
      <div id="watch-table-wrap">
        <table id="watch-table">
          <thead>
            <tr id="watch-table-head-row"></tr>
          </thead>
          <tbody id="watch-tbody"></tbody>
        </table>
      </div>
    </div>
  </section>

  <div id="minimap-wrap">
    <canvas id="minimap-canvas"></canvas>
    <div id="minimap-viewport"></div>
    <div id="cursor-readout"></div>
  </div>

  <section id="raw-log-panel" data-open="false">
    <div class="panel-resizer" title="Drag to resize"></div>
    <div class="panel-header">
      <div class="panel-title">
        <span class="panel-dot"></span>
        <span data-i18n="raw_log">原始日志</span>
      </div>
      <div class="panel-actions">
        <span id="raw-log-count" class="panel-count">0 lines</span>
        <button id="raw-log-save" class="panel-btn" data-i18n-title="save_raw_log_tip" data-i18n="save">保存</button>
        <button id="raw-log-clear" class="panel-btn" data-i18n-title="clear_log" data-i18n="clear">清除</button>
        <button id="raw-log-close" class="panel-btn panel-btn-close" data-i18n-title="close_panel" title="关闭面板">&#x2715;</button>
      </div>
    </div>
    <pre id="raw-log"></pre>
  </section>
  <section id="inspector-panel" aria-hidden="true"></section>
</main>

<div id="threshold-overlay" class="config-overlay" aria-hidden="true">
  <div class="config-dialog" role="dialog" aria-modal="true" aria-labelledby="threshold-title">
    <h2 id="threshold-title" data-i18n="thresholds">阈值</h2>
    <div class="config-grid">
      <div class="config-field full">
        <label for="threshold-channel" data-i18n="channel">通道</label>
        <select id="threshold-channel"></select>
      </div>
      <div class="config-field">
        <label for="threshold-warn-low" data-i18n="warn_low">警告下限</label>
        <input id="threshold-warn-low" type="number" step="0.1">
      </div>
      <div class="config-field">
        <label for="threshold-warn-high" data-i18n="warn_high">警告上限</label>
        <input id="threshold-warn-high" type="number" step="0.1">
      </div>
      <div class="config-field">
        <label for="threshold-alarm-low" data-i18n="alarm_low">报警下限</label>
        <input id="threshold-alarm-low" type="number" step="0.1">
      </div>
      <div class="config-field">
        <label for="threshold-alarm-high" data-i18n="alarm_high">报警上限</label>
        <input id="threshold-alarm-high" type="number" step="0.1">
      </div>
    </div>
    <div class="config-actions">
      <button id="threshold-clear" class="panel-btn" data-i18n="clear">清除</button>
      <button id="threshold-cancel" class="panel-btn" data-i18n="cancel">取消</button>
      <button id="threshold-apply" class="panel-btn" data-i18n="apply">应用</button>
    </div>
  </div>
</div>
<div id="shutdown-overlay">
  <h2 data-i18n="server_shutdown">服务器已关闭</h2>
  <p data-i18n="server_stopped_msg">可视化服务器已停止。</p>
  <p data-i18n="close_tab_msg">可以关闭此标签页。</p>
</div>

<div id="help-overlay" aria-hidden="true">
  <div id="help-modal" role="dialog" aria-modal="true" aria-labelledby="help-modal-title">
    <div id="help-modal-header">
      <h2 id="help-modal-title" data-i18n="help_title">使用说明</h2>
      <button id="help-close-btn" data-i18n-title="close_esc" title="关闭 (Esc)">&times;</button>
    </div>
    <div id="help-modal-body">
      <div class="help-section"><h3 data-i18n="help_chart">图表交互</h3><ul id="help-chart-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_var_selector">变量选择器</h3><ul id="help-var-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_trigger_sys">触发系统</h3><ul id="help-trigger-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_watch_panel">Watch 面板</h3><ul id="help-watch-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_minimap">缩略图</h3><ul id="help-minimap-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_cursors">测量光标</h3><ul id="help-cursors-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_export">数据导出</h3><ul id="help-export-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_shortcuts">键盘快捷键</h3><table class="help-kbd-table" id="help-kbd-table"></table></div>
      <div class="help-section"><h3 data-i18n="help_rawlog">Raw Log 面板</h3><ul id="help-rawlog-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_pause_resume">暂停/恢复</h3><ul id="help-pause-list"></ul></div>
    </div>
  </div>
</div>`
}

function injectScripts(el: HTMLDivElement, mode: string) {
  const minPoints = mode === 'SuperWatch' ? 50000 : 2
  const maxPoints = mode === 'SuperWatch' ? 50000 : 10000
  // 1. Set CONFIG globally
  const configScript = document.createElement('script')
  configScript.textContent = `
    var CONFIG = {
      minPoints: ${minPoints},
      maxPoints: ${maxPoints},
      title: "MKLink ${mode}",
      mode: "${mode}",
      lang: ${JSON.stringify(language.value)},
      apiBase: ${JSON.stringify(API_BASE)},
      deviceConnected: ${props.deviceConnected}
    };
  `
  el.appendChild(configScript)

  // 2. Load i18n script
  const i18nScript = document.createElement('script')
  i18nScript.src = i18nUrl
  i18nScript.onload = () => {
    // DOMContentLoaded already fired, call applyI18n manually
    if (typeof (window as any).setLang === 'function') {
      ;(window as any).setLang(language.value)
    }
    // 3. Load main viewer script after i18n
    loadViewerScript(el)
  }
  el.appendChild(i18nScript)
}

function loadViewerScript(el: HTMLDivElement) {
  const viewerScript = document.createElement('script')
  viewerScript.src = viewerUrl
  viewerScript.onload = () => {
    // Store es reference for cleanup (var es leaks to window in classic scripts)
    const viewers = (window as any).__waveformViewers
    if (viewers && !viewers[props.mode]) {
      viewers[props.mode] = { es: (window as any).es }
    } else if (viewers?.[props.mode]) {
      viewers[props.mode].es = (window as any).es
    }
    if (viewers?.[props.mode]) {
      attachSuperwatchRequesters(viewers[props.mode])
      viewers[props.mode].configureBinaryChannels?.(vofaChannels)
      applyHiddenChannels()
      if (latestVofaStatus) viewers[props.mode].updateAcquisitionStatus?.(latestVofaStatus)
      if (pendingBatch) {
        viewers[props.mode].acceptBinaryBatch?.(pendingBatch, vofaChannels)
        pendingBatch = null
      }
    }
  }
  el.appendChild(viewerScript)
}
</script>

<style scoped>
.waveform-viewer {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
</style>
