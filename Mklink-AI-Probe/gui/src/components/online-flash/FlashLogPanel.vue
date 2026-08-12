<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { tr } from '../../composables/useLanguage'
const props = defineProps<{ lines: string[]; streamDisconnected: boolean }>()
defineEmits<{ clear: []; reconnect: [] }>()
const ROW_HEIGHT = 18
const VIEWPORT_HEIGHT = 135
const OVERSCAN = 10
const FOLLOW_THRESHOLD = ROW_HEIGHT * 2
const viewport = ref<HTMLElement | null>(null)
const scrollTop = ref(0)
const followTail = ref(true)
const start = computed(() => Math.max(0, Math.floor(scrollTop.value / ROW_HEIGHT) - OVERSCAN))
const count = Math.ceil(VIEWPORT_HEIGHT / ROW_HEIGHT) + OVERSCAN * 2
const end = computed(() => Math.min(props.lines.length, start.value + count))
const visibleLines = computed(() => props.lines.slice(start.value, end.value))
const paddingTop = computed(() => start.value * ROW_HEIGHT)
const paddingBottom = computed(() => Math.max(0, (props.lines.length - end.value) * ROW_HEIGHT))
function scrolled(event: Event) {
  const element = event.currentTarget as HTMLElement
  scrollTop.value = element.scrollTop
  followTail.value = element.scrollHeight - element.clientHeight - element.scrollTop <= FOLLOW_THRESHOLD
}
async function moveToLatest(): Promise<void> {
  await nextTick()
  if (!followTail.value || !viewport.value) return
  const top = Math.max(0, viewport.value.scrollHeight - viewport.value.clientHeight)
  viewport.value.scrollTop = top
  scrollTop.value = top
}
function jumpToLatest(): void { followTail.value = true; void moveToLatest() }
watch(() => props.lines.length, () => { if (followTail.value) void moveToLatest() }, { flush: 'post' })
onMounted(() => { if (followTail.value) void moveToLatest() })
async function copy() { await navigator.clipboard?.writeText(props.lines.join('\n')) }
function exportLog() { const url = URL.createObjectURL(new Blob([props.lines.join('\n')], {type:'text/plain'})); const link = document.createElement('a'); link.href=url; link.download='online-flash.log'; link.click(); URL.revokeObjectURL(url) }
</script>
<template>
  <header><h3>{{ tr('任务日志', 'Job Log') }} <span>{{ lines.length }}/5000</span></h3><div><button v-if="!followTail" data-testid="jump-latest" @click="jumpToLatest">{{ tr('跳到最新', 'Jump to Latest') }}</button><button v-if="streamDisconnected" data-testid="reconnect-stream" @click="$emit('reconnect')">{{ tr('从断点重连', 'Reconnect from Cursor') }}</button><button @click="copy">{{ tr('复制', 'Copy') }}</button><button @click="exportLog">{{ tr('导出', 'Export') }}</button><button @click="$emit('clear')">{{ tr('清空', 'Clear') }}</button></div></header>
  <div ref="viewport" data-testid="log-viewport" class="log-window" @scroll="scrolled"><p v-if="!visibleLines.length">{{ tr('等待在线烧录任务', 'Waiting for an online flash job') }}</p><div :style="{height:`${paddingTop}px`}"/><div v-for="(line,index) in visibleLines" :key="start + index" class="log-line" data-testid="log-line" :title="line" :aria-label="line">{{ line }}</div><div :style="{height:`${paddingBottom}px`}"/></div>
</template>
<style scoped>
header{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border-bottom:1px solid var(--of-border);background:var(--surface-muted)}h3{margin:0;font-size:12px}h3 span{color:var(--of-muted);font-weight:400}button{margin-left:5px;padding:4px 7px;border:1px solid var(--control-border);border-radius:4px;background:var(--of-input);color:var(--of-text);font-size:10px}.log-window{height:135px;overflow:auto;padding:0 10px;text-align:left;background:var(--terminal-bg);color:var(--terminal-text);font:10px/18px var(--of-mono)}.log-line{height:18px;line-height:18px;box-sizing:border-box;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.log-window p{color:#a8bbb8}
</style>
