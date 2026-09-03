<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowUpFromLine, Save, Upload, X } from '@lucide/vue'
import { useOnlineFlashApi } from '../../composables/useOnlineFlashApi'
import { tr } from '../../composables/useLanguage'
import { downloadBlobFile } from '../../lib/downloadTextFile'
import type { TargetMemoryRegion } from '../../types/onlineFlash'

const props = defineProps<{
  probeId: string
  targetPart: string
  hpm: boolean
  board?: string
  frequency: number
  connectMode: string
  resetMode: string
  memoryRegions?: TargetMemoryRegion[]
  memoryMapBusy?: boolean
  disabled?: boolean
  embedded?: boolean
}>()

const emit = defineEmits<{
  progress: [value: number, text: string, state: 'waiting' | 'reading' | 'done' | 'failed']
  log: [line: string]
  data: [payload: { address: number; data: Uint8Array }]
  clear: []
}>()

const GENERIC_FLASH_START = '0x08000000'
const GENERIC_FLASH_END = '0x08080000'
const HPM_FLASH_START = '0x80000000'
const HPM_FLASH_END = '0x80080000'
// HPM XPI Flash uses 4 KiB sectors. Keep each host request sector-sized;
// the probe may still split the request into its internal 2 KiB frames.
const HPM_READ_CHUNK_SIZE = 4 * 1024
const address = ref(GENERIC_FLASH_START)
const endAddress = ref(GENERIC_FLASH_END)
const rangeTarget = ref('')
const rangeDirty = ref(false)
const busy = ref(false)
const error = ref('')
const readDialogOpen = ref(false)
const progress = ref(0)
const progressText = ref('')
const data = ref<Uint8Array | null>(null)
type ProgressState = 'waiting' | 'reading' | 'done' | 'failed'
const progressEntries = ref<Array<{ address: number; size: number; state: ProgressState }>>([])
const loggedEntries = new Set<string>()
const api = useOnlineFlashApi()

const flashRange = computed(() => {
  const regions = (props.memoryRegions || []).filter(region => (
    Number.isInteger(region.start) && Number.isInteger(region.length) && region.length > 0
  ))
  if (!regions.length) return null
  const start = Math.min(...regions.map(region => region.start))
  const end = Math.max(...regions.map(region => region.start + region.length))
  return end > start ? { start, end } : null
})

function formatAddress(value: number): string {
  return `0x${value.toString(16).toUpperCase().padStart(8, '0')}`
}

function applyRangeDefaults(): void {
  const target = props.targetPart.trim()
  const range = flashRange.value
  const fallback = props.hpm
    ? { start: Number.parseInt(HPM_FLASH_START.slice(2), 16), end: Number.parseInt(HPM_FLASH_END.slice(2), 16) }
    : null
  if (!target || (!range && !fallback) || (rangeDirty.value && rangeTarget.value === target)) return
  const selected = range || fallback!
  address.value = formatAddress(selected.start)
  endAddress.value = formatAddress(selected.end)
  rangeTarget.value = target
  rangeDirty.value = false
}

watch(() => props.targetPart, target => {
  rangeTarget.value = target.trim()
  rangeDirty.value = false
  applyRangeDefaults()
}, { immediate: true })
watch(() => props.hpm, () => {
  rangeDirty.value = false
  applyRangeDefaults()
})
watch(flashRange, applyRangeDefaults, { immediate: true })

const parsedAddress = computed(() => {
  if (!/^0x[0-9a-f]+$/i.test(address.value.trim())) return null
  const value = Number.parseInt(address.value.trim().slice(2), 16)
  return Number.isSafeInteger(value) && value >= 0 && value <= 0xffff_ffff ? value : null
})
const parsedEndAddress = computed(() => {
  if (!/^0x[0-9a-f]+$/i.test(endAddress.value.trim())) return null
  const value = Number.parseInt(endAddress.value.trim().slice(2), 16)
  return Number.isSafeInteger(value) && value >= 0 && value <= 0xffff_ffff ? value : null
})
const readSize = computed(() => parsedAddress.value !== null && parsedEndAddress.value !== null
  ? parsedEndAddress.value - parsedAddress.value : 0)
const canRead = computed(() => (
  !!props.probeId && !!props.targetPart
  && readSize.value > 0
  && readSize.value <= 64 * 1024 * 1024
  && !busy.value && !props.disabled && !props.memoryMapBusy
))

const sectorSizes = computed(() => Array.from(new Set(
  (props.memoryRegions || []).map(region => region.sector_size).filter(size => size > 0),
)).sort((left, right) => left - right))
const chunkDescription = computed(() => sectorSizes.value.length
  ? tr(
      `按目标 Flash 扇区分块（${sectorSizes.value.join(' / ')} 字节）。`,
      `Uses target Flash sector chunks (${sectorSizes.value.join(' / ')} bytes).`,
    )
  : tr('未取得目标扇区信息，将按 4096 字节分块。', 'Target sector geometry is unavailable; uses 4096-byte chunks.'))

function chunkSizeAt(address: number, remaining: number): number {
  if (props.hpm) return Math.min(HPM_READ_CHUNK_SIZE, remaining)
  const region = (props.memoryRegions || []).find(item => (
    Number.isInteger(item.start) && Number.isInteger(item.length) && item.length > 0
    && Number.isInteger(item.sector_size) && item.sector_size > 0
    && address >= item.start && address < item.start + item.length
  ))
  if (!region) return Math.min(4 * 1024, remaining)
  const available = region.start + region.length - address
  return Math.min(remaining, region.sector_size, available)
}

function openReadDialog(): void {
  error.value = ''
  readDialogOpen.value = true
}

function closeReadDialog(): void {
  if (!busy.value) readDialogOpen.value = false
}

function clearMemory(notify = true): void {
  data.value = null
  progress.value = 0
  progressText.value = ''
  progressEntries.value = []
  error.value = ''
  loggedEntries.clear()
  if (notify) emit('clear')
}

async function readMemory(): Promise<void> {
  if (!canRead.value || parsedAddress.value === null || parsedEndAddress.value === null) return
  error.value = ''
  busy.value = true
  progress.value = 0
  progressText.value = ''
  data.value = null
  progressEntries.value = []
  loggedEntries.clear()
  emit('progress', 0, '', 'reading')
  const start = parsedAddress.value
  const end = parsedEndAddress.value
  const total = end - start
  for (let offset = 0; offset < total;) {
    const size = chunkSizeAt(start + offset, total - offset)
    progressEntries.value.push({ address: start + offset, size, state: offset === 0 ? 'reading' : 'waiting' })
    offset += size
  }
  try {
    const blob = await api.readMemoryStream({
      address: `0x${start.toString(16)}`,
      size: total,
      chunk_sizes: progressEntries.value.map(entry => entry.size),
      probe_id: props.probeId,
      target_part: props.targetPart,
      frequency: props.frequency,
      connect_mode: props.connectMode,
      reset_mode: props.resetMode,
      board: props.board || null,
    }, received => {
      let completed = 0
      for (const entry of progressEntries.value) {
        completed += entry.size
        entry.state = received >= completed ? 'done' : received > completed - entry.size ? 'reading' : 'waiting'
        const key = `${entry.address}-${entry.size}`
        if (entry.state === 'done' && !loggedEntries.has(key)) {
          loggedEntries.add(key)
          emit('log', `[READ] ${tr('读取完成', 'Read complete')} 0x${entry.address.toString(16).toUpperCase().padStart(8, '0')} · ${entry.size} Bytes`)
        }
      }
      progress.value = Math.min(received / total, 1)
      progressText.value = `${Math.min(received, total)} / ${total} bytes`
      emit('progress', progress.value, progressText.value, 'reading')
    })
    const result = new Uint8Array(await blob.arrayBuffer())
    if (result.length !== total) throw new Error(tr('读取数据长度不匹配', 'Read returned an unexpected length'))
    progressEntries.value.forEach(entry => { entry.state = 'done' })
    progress.value = 1
    progressText.value = `${total} / ${total} bytes`
    data.value = result
    emit('data', { address: start, data: result })
    emit('progress', 1, progressText.value, 'done')
    emit('log', `[READ] ${tr('读取完成', 'Read complete')} 0x${start.toString(16).toUpperCase().padStart(8, '0')} - 0x${end.toString(16).toUpperCase().padStart(8, '0')} · ${total} Bytes`)
  } catch (caught) {
    const activeEntry = progressEntries.value.find(entry => entry.state === 'reading')
    if (activeEntry) activeEntry.state = 'failed'
    error.value = caught instanceof Error ? caught.message : String(caught)
    emit('progress', progress.value, error.value, 'failed')
    emit('log', `[READ] ${tr('读取失败', 'Read failed')}：${error.value}`)
  } finally {
    busy.value = false
  }
}

async function saveMemory(): Promise<void> {
  if (!data.value || parsedAddress.value === null) return
  const filename = `read-0x${parsedAddress.value.toString(16).padStart(8, '0').toUpperCase()}-${data.value.length}.bin`
  const blob = new Blob([data.value.buffer.slice(data.value.byteOffset, data.value.byteOffset + data.value.byteLength) as ArrayBuffer], { type: 'application/octet-stream' })
  try {
    const picker = (window as Window & { showSaveFilePicker?: (options?: unknown) => Promise<{ createWritable: () => Promise<{ write: (value: Blob) => Promise<void>; close: () => Promise<void> }> }> }).showSaveFilePicker
    if (picker) {
      const handle = await picker({ suggestedName: filename, types: [{ description: 'Binary file', accept: { 'application/octet-stream': ['.bin'] } }] })
      const writable = await handle.createWritable()
      await writable.write(blob)
      await writable.close()
      return
    }
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === 'AbortError') return
    error.value = caught instanceof Error ? caught.message : String(caught)
    return
  }
  downloadBlobFile(filename, blob)
}

defineExpose({ clearMemory: () => clearMemory(false), openReadDialog, saveMemory })
</script>

<template>
  <section v-if="!embedded" class="memory-read-panel" data-testid="memory-read-panel">
    <header><h3>{{ tr('读取目标数据', 'Read Target Data') }}</h3><span v-if="hpm" class="badge">HPM</span></header>
    <p v-if="hpm" class="memory-read-note">{{ tr('HPM 使用 dump_memory 二进制读取，按 32 KiB 分块。', 'HPM uses binary dump_memory reads in 32 KiB chunks.') }}</p>
      <div class="memory-read-actions">
        <button class="btn" type="button" data-testid="memory-read-submit" :disabled="!canRead" @click="openReadDialog"><Upload :size="14" aria-hidden="true" />{{ tr('读取数据', 'Read Data') }}</button>
        <button class="btn" type="button" data-testid="memory-read-save" :disabled="!data || busy" @click="saveMemory"><Save :size="14" aria-hidden="true" />{{ tr('保存文件', 'Save File') }}</button>
      </div>
      <div v-if="busy || data" class="memory-read-progress" data-testid="memory-read-progress">
        <div class="memory-read-progress-row"><span>{{ busy ? tr('读取进度', 'Read progress') : tr('读取完成', 'Read complete') }}</span><span>{{ Math.round(progress * 100) }}%</span></div>
        <progress :value="progress" max="1"></progress>
        <span class="memory-read-note">{{ progressText || `${data?.length || 0} bytes` }}</span>
        <div class="memory-read-log" data-testid="memory-read-log">
          <div v-for="entry in progressEntries" :key="`${entry.address}-${entry.size}`" class="memory-read-log-entry">
            <span :class="`memory-read-log-state ${entry.state}`">{{ entry.state === 'done' ? tr('读取完成', 'Read complete') : entry.state === 'failed' ? tr('读取失败', 'Read failed') : entry.state === 'reading' ? tr('正在读取...', 'Reading...') : tr('等待读取', 'Waiting') }}</span>
            <span class="memory-read-log-range">0x{{ entry.address.toString(16).toUpperCase().padStart(8, '0') }} · {{ entry.size }} Bytes</span>
          </div>
        </div>
      </div>
      <p v-if="error" class="memory-read-error" role="alert">{{ error }}</p>
  </section>

  <div v-if="readDialogOpen" class="memory-read-dialog-backdrop" role="presentation" @click.self="closeReadDialog">
    <section class="memory-read-dialog" role="dialog" aria-modal="true" aria-labelledby="memory-read-dialog-title">
      <header class="memory-read-dialog-header">
        <div class="memory-read-dialog-heading">
          <span class="memory-read-dialog-icon" aria-hidden="true"><ArrowUpFromLine :size="17" /></span>
          <div><h4 id="memory-read-dialog-title">{{ tr('读取目标 Flash', 'Read Target Flash') }}</h4><p>{{ tr('填写要读取的地址范围', 'Choose the address range to read') }}</p></div>
        </div>
        <button class="icon-button" type="button" :title="tr('关闭', 'Close')" @click="closeReadDialog"><X :size="16" aria-hidden="true" /></button>
      </header>
      <div class="memory-read-range-fields">
        <label><span>{{ tr('基地址', 'Base Address') }}</span><div class="memory-read-input-wrap"><input v-model.trim="address" data-testid="memory-read-address" inputmode="text" spellcheck="false" placeholder="0x08000000" @input="rangeDirty = true"><small>起始（含）</small></div></label>
        <span class="memory-read-range-arrow" aria-hidden="true">→</span>
        <label><span>{{ tr('结束地址', 'End Address') }}</span><div class="memory-read-input-wrap"><input v-model.trim="endAddress" data-testid="memory-read-end-address" inputmode="text" spellcheck="false" placeholder="0x08080000" @input="rangeDirty = true"><small>结束（不含）</small></div></label>
      </div>
      <div v-if="readSize > 0" class="memory-read-summary"><span>{{ tr('读取范围', 'Read range') }}</span><strong>0x{{ parsedAddress?.toString(16).toUpperCase().padStart(8, '0') }} – 0x{{ parsedEndAddress?.toString(16).toUpperCase().padStart(8, '0') }}</strong><em>{{ readSize.toLocaleString() }} bytes</em></div>
      <p v-else class="memory-read-validation">{{ tr('结束地址必须大于基地址，并使用 0x 十六进制格式。', 'The end address must be greater than the base address and use 0x hexadecimal format.') }}</p>
      <p class="memory-read-note">{{ readSize > 0 ? (hpm ? tr('HPM 固件内部按 2048 字节帧传输。', 'HPM firmware transfers 2048-byte frames internally.') : chunkDescription) : '' }}</p>
      <div class="memory-read-dialog-actions"><button class="btn" type="button" @click="closeReadDialog">{{ tr('取消', 'Cancel') }}</button><button class="btn btn-primary" type="button" data-testid="memory-read-confirm" :disabled="!canRead" @click="readDialogOpen = false; void readMemory()"><Upload :size="14" aria-hidden="true" />{{ tr('开始读取', 'Start Read') }}</button></div>
    </section>
  </div>
</template>

<style scoped>
.memory-read-panel { display: grid; gap: 8px; padding: 10px; border-top: 1px solid var(--of-border); }
.memory-read-panel header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.memory-read-panel h3 { margin: 0; color: var(--of-text); font-size: 12px; }
.memory-read-panel label { display: grid; gap: 4px; color: var(--of-muted); }
.memory-read-panel input { min-width: 0; width: 100%; height: 30px; box-sizing: border-box; border: 1px solid var(--of-border); border-radius: 5px; background: var(--of-input); color: var(--of-text); padding: 0 8px; font-family: var(--of-mono); }
.memory-read-panel .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.memory-read-actions { display: flex; gap: 8px; }
.memory-read-actions .btn { flex: 1 1 0; min-width: 0; }
.memory-read-progress { display: grid; gap: 5px; }
.memory-read-progress-row { display: flex; justify-content: space-between; color: var(--of-text); font-variant-numeric: tabular-nums; }
.memory-read-progress progress { width: 100%; height: 8px; accent-color: var(--of-accent); }
.memory-read-note { margin: 0; color: var(--of-muted); line-height: 1.4; }
.memory-read-log { display: grid; gap: 2px; max-height: 150px; overflow: auto; padding: 5px 7px; border: 1px solid var(--of-border); border-radius: 4px; background: var(--of-input); font-family: var(--of-mono); font-size: 11px; }
.memory-read-log-entry { display: flex; gap: 8px; min-width: 0; line-height: 1.45; }
.memory-read-log-state { flex: 0 0 auto; color: var(--of-muted); }
.memory-read-log-state.done { color: var(--of-ok); }
.memory-read-log-state.failed { color: var(--of-danger); }
.memory-read-log-range { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--of-text); }
.memory-read-error { margin: 0; color: var(--of-danger); overflow-wrap: anywhere; }
.memory-read-dialog-backdrop { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 16px; background: rgb(0 0 0 / 42%); }
.memory-read-dialog { --memory-dialog-bg: var(--of-surface, #1d2229); --memory-dialog-input: var(--of-input, #252b33); --memory-dialog-border: var(--of-border, #34404d); --memory-dialog-text: var(--of-text, #e6e9ed); --memory-dialog-muted: var(--of-muted, #929ba7); --memory-dialog-accent: var(--of-accent, #58a6d6); width: min(520px, 100%); display: grid; gap: 16px; padding: 22px; border: 1px solid var(--memory-dialog-border); border-radius: 10px; background: var(--memory-dialog-bg); color: var(--memory-dialog-text); box-shadow: 0 24px 70px rgb(0 0 0 / 54%); }
.memory-read-dialog > .memory-read-dialog-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; padding: 0 0 14px; border-bottom: 1px solid var(--memory-dialog-border); background: transparent; color: var(--memory-dialog-text); flex-shrink: 1; flex-wrap: nowrap; }
.memory-read-dialog-heading { display: flex; align-items: center; gap: 11px; min-width: 0; }
.memory-read-dialog-icon { display: inline-grid; place-items: center; width: 34px; height: 34px; flex: 0 0 auto; border: 1px solid color-mix(in srgb, var(--memory-dialog-accent) 48%, var(--memory-dialog-border)); border-radius: 8px; background: color-mix(in srgb, var(--memory-dialog-accent) 16%, var(--memory-dialog-input)); color: var(--memory-dialog-accent); }
.memory-read-dialog h4 { margin: 0; color: var(--memory-dialog-text); font-size: 16px; letter-spacing: 0; }
.memory-read-dialog-heading p { margin: 3px 0 0; color: var(--memory-dialog-muted); font-size: 11px; }
.memory-read-dialog label { display: grid; gap: 7px; min-width: 0; color: var(--memory-dialog-muted); font-size: 12px; font-weight: 600; }
.memory-read-input-wrap { position: relative; min-width: 0; }
.memory-read-dialog input { min-width: 0; width: 100%; height: 42px; box-sizing: border-box; border: 1px solid var(--memory-dialog-border); border-radius: 6px; background: var(--memory-dialog-input); color: var(--memory-dialog-text); padding: 0 10px; font-family: var(--of-mono, ui-monospace, Consolas, monospace); font-size: 14px; font-weight: 600; letter-spacing: .02em; }
.memory-read-dialog input:focus { outline: 2px solid color-mix(in srgb, var(--memory-dialog-accent) 65%, transparent); outline-offset: 1px; border-color: var(--memory-dialog-accent); }
.memory-read-input-wrap small { display: block; margin-top: 4px; color: var(--memory-dialog-muted); font-size: 10px; font-weight: 400; }
.memory-read-range-fields { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: end; gap: 10px; }
.memory-read-range-arrow { padding-bottom: 26px; color: var(--memory-dialog-accent); font: 600 18px/1 var(--of-mono, ui-monospace, Consolas, monospace); }
.memory-read-summary { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--memory-dialog-accent) 30%, var(--memory-dialog-border)); border-radius: 6px; background: color-mix(in srgb, var(--memory-dialog-accent) 9%, var(--memory-dialog-input)); color: var(--memory-dialog-muted); font-size: 11px; }
.memory-read-summary strong { min-width: 0; overflow: hidden; color: var(--memory-dialog-text); font: 600 11px/1.4 var(--of-mono, ui-monospace, Consolas, monospace); text-overflow: ellipsis; white-space: nowrap; }
.memory-read-summary em { color: var(--memory-dialog-accent); font-style: normal; font-variant-numeric: tabular-nums; white-space: nowrap; }
.memory-read-validation { margin: 0; padding: 9px 11px; border: 1px solid color-mix(in srgb, var(--of-danger, #f07178) 34%, var(--memory-dialog-border)); border-radius: 6px; background: var(--of-danger-bg, #3b2428); color: var(--of-danger, #f07178); font-size: 11px; }
.memory-read-dialog .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.memory-read-dialog .icon-button { display: inline-grid; place-items: center; width: 30px; height: 30px; flex: 0 0 auto; padding: 0; border: 1px solid var(--memory-dialog-border); border-radius: 6px; background: color-mix(in srgb, var(--memory-dialog-input) 70%, transparent); color: var(--memory-dialog-muted); cursor: pointer; }
.memory-read-dialog .icon-button:hover { border-color: var(--memory-dialog-accent); color: var(--memory-dialog-text); }
.memory-read-dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 2px; padding-top: 14px; border-top: 1px solid var(--memory-dialog-border); }
.memory-read-dialog-actions .btn { min-width: 96px; min-height: 36px; border: 1px solid var(--memory-dialog-border); border-radius: 5px; cursor: pointer; }
.memory-read-dialog-actions .btn:not(.btn-primary) { background: #eef1f4; color: #20262d; }
.memory-read-dialog-actions .btn:not(.btn-primary):hover { background: #fff; }
.memory-read-dialog-actions .btn.btn-primary { border-color: #e58a3a; background: #e58a3a; color: #fff; }
.memory-read-dialog-actions .btn.btn-primary:hover:not(:disabled) { background: #f09a4d; border-color: #f09a4d; }
.memory-read-dialog-actions .btn:disabled { cursor: not-allowed; opacity: .45; }
@media (max-width: 540px) { .memory-read-dialog { padding: 16px; } .memory-read-range-fields { grid-template-columns: 1fr; gap: 8px; } .memory-read-range-arrow { display: none; } .memory-read-summary { grid-template-columns: 1fr; gap: 3px; } .memory-read-summary em { justify-self: start; } }
</style>
