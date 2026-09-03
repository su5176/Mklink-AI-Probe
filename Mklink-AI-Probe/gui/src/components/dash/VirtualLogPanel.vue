<template>
  <div
    ref="viewport" class="virtual-log" role="log" tabindex="0"
    aria-live="off" @scroll="onScroll"
  >
    <div class="virtual-log-spacer" :style="{ height: `${entries.length * rowHeight}px` }">
      <div class="virtual-log-window" :style="{ transform: `translateY(${firstVisible * rowHeight}px)` }">
        <div
          v-for="entry in visibleEntries" :key="entry.number"
          class="virtual-log-row" :class="`level-${entry.level}`"
        >
          <span class="virtual-log-number">{{ entry.number }}</span>
          <span class="virtual-log-time">{{ formatTime(entry.time) }}</span>
          <span class="virtual-log-level">{{ entry.label || entry.level }}</span>
          <span class="virtual-log-text">{{ entry.text }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'

export interface VirtualLogInput {
  readonly time: number | bigint
  readonly level: 'raw' | 'data' | 'warning' | 'error'
  readonly text: string
  readonly label?: string
}

interface VirtualLogEntry extends VirtualLogInput { readonly number: number }

const MAX_LINES = 5000
const FOLLOW_THRESHOLD = 24
const FLUSH_INTERVAL_MS = 33
const rowHeight = 22
const overscan = 6
const viewport = ref<HTMLElement | null>(null)
const entries = ref<VirtualLogEntry[]>([])
const scrollTop = ref(0)
const viewportHeight = ref(300)
const following = ref(true)
let nextNumber = 1
let pending: VirtualLogInput[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null
let resizeObserver: ResizeObserver | null = null

const retainedCount = computed(() => entries.value.length)
const firstLineNumber = computed(() => entries.value[0]?.number ?? 0)
const firstVisible = computed(() => Math.max(0, Math.floor(scrollTop.value / rowHeight) - overscan))
const visibleCount = computed(() => Math.ceil(viewportHeight.value / rowHeight) + overscan * 2)
const visibleEntries = computed(() => entries.value.slice(
  firstVisible.value, firstVisible.value + visibleCount.value,
))

function append(batch: readonly VirtualLogInput[]): void {
  if (!batch.length) return
  pending.push(...batch)
  if (flushTimer === null) flushTimer = setTimeout(flush, FLUSH_INTERVAL_MS)
}

function flush(): void {
  flushTimer = null
  if (!pending.length) return
  const next = pending.map(entry => ({ ...entry, number: nextNumber++ }))
  pending = []
  entries.value = entries.value.concat(next).slice(-MAX_LINES)
  if (following.value) void nextTick(scrollToBottom)
}

function clear(): void {
  entries.value = []
  pending = []
  nextNumber = 1
  following.value = true
  scrollTop.value = 0
  if (flushTimer !== null) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
}

function exportText(): string {
  flush()
  return entries.value.map(entry => (
    `${formatTime(entry.time)}\t${entry.label || entry.level}\t${entry.text}`
  )).join('\n')
}

function scrollToBottom(): void {
  const element = viewport.value
  if (!element || !following.value) return
  element.scrollTop = element.scrollHeight
  scrollTop.value = element.scrollTop
}

function onScroll(): void {
  const element = viewport.value
  if (!element) return
  scrollTop.value = element.scrollTop
  following.value = element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_THRESHOLD
}

function measure(): void {
  if (viewport.value?.clientHeight) viewportHeight.value = viewport.value.clientHeight
}

function formatTime(value: number | bigint): string {
  const milliseconds = typeof value === 'bigint' ? Number(value / 1_000_000n) : value * 1000
  if (!Number.isFinite(milliseconds)) return '--:--:--.---'
  const date = new Date(milliseconds)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:` +
    `${String(date.getSeconds()).padStart(2, '0')}.${String(date.getMilliseconds()).padStart(3, '0')}`
}

onMounted(() => {
  measure()
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(measure)
    if (viewport.value) resizeObserver.observe(viewport.value)
  }
  window.addEventListener('resize', measure)
})

onUnmounted(() => {
  if (flushTimer !== null) clearTimeout(flushTimer)
  flushTimer = null
  pending = []
  resizeObserver?.disconnect()
  window.removeEventListener('resize', measure)
})

defineExpose({ append, clear, exportText, retainedCount, firstLineNumber, following })
</script>

<style scoped>
.virtual-log { position: relative; overflow: auto; min-height: 160px; background: #1e1e1e; color: #ccc; font: 12px/22px var(--font-mono); }
.virtual-log-spacer { position: relative; min-width: max-content; width: 100%; }
.virtual-log-window { position: absolute; inset: 0 auto auto 0; min-width: 100%; }
.virtual-log-row { display: grid; grid-template-columns: 56px 96px 56px minmax(max-content, 1fr); gap: 8px; height: 22px; white-space: pre; }
.virtual-log-number, .virtual-log-time, .virtual-log-level { color: var(--dim); user-select: none; }
.virtual-log-number { text-align: right; }
.level-data .virtual-log-text { color: #8be9fd; }
.level-warning .virtual-log-text { color: var(--warn); }
.level-error .virtual-log-text { color: var(--danger, #ef4444); }
</style>
