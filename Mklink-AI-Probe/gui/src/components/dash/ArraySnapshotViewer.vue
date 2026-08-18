<template>
  <section class="array-snapshot-viewer" data-testid="array-snapshot-viewer">
    <header>
      <div class="snapshot-title">
        <Activity :size="16" aria-hidden="true" />
        <strong>{{ snapshot?.name || path }}</strong>
        <span>{{ tr('数组快照', 'Array Snapshot') }}</span>
      </div>
      <div class="snapshot-status">
        <span v-if="snapshot">{{ snapshot.count }} × {{ snapshot.type_name }}</span>
        <span v-if="snapshot?.sequence">#{{ snapshot.sequence }}</span>
        <button type="button" data-testid="close-array-snapshot" @click="clearSnapshot">
          {{ tr('关闭', 'Close') }}
        </button>
      </div>
    </header>
    <div class="snapshot-chart-wrap">
      <canvas ref="canvas" :aria-label="tr(`${path} 数组快照曲线`, `${path} array snapshot curve`)"></canvas>
      <div v-if="!deviceConnected" class="snapshot-empty">{{ tr('设备未连接', 'Device not connected') }}</div>
      <div v-else-if="error" class="snapshot-empty snapshot-error">{{ error }}</div>
      <div v-else-if="!snapshot?.values.length" class="snapshot-empty">
        {{ tr('等待数组采样…', 'Waiting for array samples…') }}
      </div>
    </div>
    <footer v-if="snapshot?.values.length">
      <span>0</span>
      <span>{{ tr('索引', 'Index') }}</span>
      <span>{{ snapshot.values.length - 1 }}</span>
      <span>{{ tr('最小', 'Min') }} {{ formatValue(valueRange.min) }}</span>
      <span>{{ tr('最大', 'Max') }} {{ formatValue(valueRange.max) }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Activity } from '@lucide/vue'
import { tr } from '../../composables/useLanguage'
import { API_BASE } from '../../lib/runtimeEndpoint'

interface ArraySnapshot {
  name: string
  type_name: string
  address: number
  element_size: number
  count: number
  sequence: number
  timestamp_us: number | null
  values: number[]
}

const props = defineProps<{
  path: string
  deviceConnected: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const canvas = ref<HTMLCanvasElement>()
const snapshot = ref<ArraySnapshot | null>(null)
const error = ref('')
let pollTimer: ReturnType<typeof setTimeout> | null = null
let resizeObserver: ResizeObserver | null = null
let disposed = false

const valueRange = computed(() => {
  const values = snapshot.value?.values ?? []
  if (!values.length) return { min: 0, max: 0 }
  return {
    min: Math.min(...values),
    max: Math.max(...values),
  }
})

function formatValue(value: number): string {
  if (!Number.isFinite(value)) return '--'
  if (Number.isInteger(value)) return String(value)
  return value.toPrecision(6)
}

function chartColor(name: string, fallback: string): string {
  const value = getComputedStyle(canvas.value || document.documentElement)
    .getPropertyValue(name)
    .trim()
  return value || fallback
}

function draw(): void {
  const target = canvas.value
  const values = snapshot.value?.values ?? []
  if (!target || !values.length) return
  const bounds = target.getBoundingClientRect()
  const width = Math.max(320, Math.round(bounds.width || target.clientWidth || 640))
  const height = Math.max(150, Math.round(bounds.height || target.clientHeight || 220))
  const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1))
  target.width = Math.round(width * ratio)
  target.height = Math.round(height * ratio)
  const context = target.getContext('2d')
  if (!context) return
  context.setTransform(ratio, 0, 0, ratio, 0, 0)
  context.clearRect(0, 0, width, height)

  const left = 48
  const right = 12
  const top = 12
  const bottom = 24
  const plotWidth = Math.max(1, width - left - right)
  const plotHeight = Math.max(1, height - top - bottom)
  const foreground = chartColor('--fg', '#263238')
  const muted = chartColor('--muted', '#7a8087')
  const border = chartColor('--border', '#d8d5ce')
  const accent = chartColor('--accent', '#d95532')

  context.strokeStyle = border
  context.lineWidth = 1
  context.beginPath()
  for (let line = 0; line <= 4; line += 1) {
    const y = top + plotHeight * line / 4
    context.moveTo(left, y)
    context.lineTo(left + plotWidth, y)
  }
  context.stroke()

  let min = valueRange.value.min
  let max = valueRange.value.max
  if (min === max) {
    const padding = Math.max(1, Math.abs(min) * 0.05)
    min -= padding
    max += padding
  }
  const span = max - min
  const xAt = (index: number) => left + plotWidth * index / Math.max(1, values.length - 1)
  const yAt = (value: number) => top + plotHeight * (max - value) / span

  context.strokeStyle = accent
  context.lineWidth = 1.5
  context.beginPath()
  values.forEach((value, index) => {
    const x = xAt(index)
    const y = yAt(value)
    if (index === 0) context.moveTo(x, y)
    else context.lineTo(x, y)
  })
  context.stroke()

  context.fillStyle = foreground
  context.font = '11px ui-monospace, Consolas, monospace'
  context.textAlign = 'right'
  context.fillText(formatValue(max), left - 6, top + 4)
  context.fillText(formatValue(min), left - 6, top + plotHeight)
  context.fillStyle = muted
}

function schedulePoll(delay = 50): void {
  if (disposed) return
  if (pollTimer !== null) clearTimeout(pollTimer)
  pollTimer = setTimeout(() => void pollSnapshot(), delay)
}

async function pollSnapshot(): Promise<void> {
  if (disposed || !props.deviceConnected) {
    schedulePoll(250)
    return
  }
  try {
    const response = await fetch(`${API_BASE}/api/dash/superwatch/array-snapshot`)
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(payload?.detail || response.statusText)
    const next = payload?.snapshot
    if (!next || next.name !== props.path) {
      snapshot.value = null
      error.value = tr('数组快照已关闭', 'Array snapshot is closed')
    } else {
      snapshot.value = next
      error.value = ''
      await nextTick()
      draw()
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    schedulePoll()
  }
}

async function clearSnapshot(): Promise<void> {
  try {
    const response = await fetch(`${API_BASE}/api/dash/superwatch/array-snapshot/clear`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload?.detail || response.statusText)
    }
    emit('close')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

function restartPolling(): void {
  snapshot.value = null
  error.value = ''
  schedulePoll(0)
}

onMounted(() => {
  if (typeof ResizeObserver !== 'undefined' && canvas.value) {
    resizeObserver = new ResizeObserver(draw)
    resizeObserver.observe(canvas.value)
  } else {
    window.addEventListener('resize', draw)
  }
  restartPolling()
})

watch(() => props.path, restartPolling)
watch(() => props.deviceConnected, restartPolling)

onUnmounted(() => {
  disposed = true
  if (pollTimer !== null) clearTimeout(pollTimer)
  resizeObserver?.disconnect()
  window.removeEventListener('resize', draw)
})
</script>

<style scoped>
.array-snapshot-viewer {
  display: grid;
  grid-template-rows: auto minmax(150px, 1fr) auto;
  min-height: 190px;
  overflow: hidden;
  border-top: 1px solid var(--border);
  background: var(--surface);
}
header, footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 34px;
  padding: 5px 10px;
  color: var(--muted);
  font-size: 11px;
}
.snapshot-title, .snapshot-status { display: flex; align-items: center; gap: 8px; min-width: 0; }
.snapshot-title strong { overflow: hidden; color: var(--fg); font: 12px Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.snapshot-title svg { flex: none; color: var(--accent); }
.snapshot-status button {
  min-height: 24px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
}
.snapshot-chart-wrap { position: relative; min-height: 0; }
canvas { display: block; width: 100%; height: 100%; }
.snapshot-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--muted);
  font-size: 12px;
  pointer-events: none;
}
.snapshot-error { color: var(--danger); }
footer { justify-content: flex-start; border-top: 1px solid var(--border); }
footer span:nth-child(2) { margin-right: auto; }
</style>
