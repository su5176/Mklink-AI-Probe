<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { CustomFlmRecord, FlashAlgorithmRecord, PackStatus, TargetRecord } from '../../types/onlineFlash'
import { tr } from '../../composables/useLanguage'

const props = defineProps<{ targets: TargetRecord[]; query: string; selectedPart: string; selectedInstalled: boolean; status: PackStatus | null; busy: boolean; cancelPending: boolean; progress: number; phase: string; error: string; algorithms: CustomFlmRecord[]; flashAlgorithms?: FlashAlgorithmRecord[]; algorithmBusy: boolean; algorithmError: string; canManageAlgorithms: boolean; algorithmNotRequired: boolean }>()
const emit = defineEmits<{ search: [value: string]; 'update:query': [value: string]; select: [target: TargetRecord]; updateIndex: []; importPack: [file: File]; cancel: []; addAlgorithm: [file: File]; removeAlgorithm: [algorithmId: string] }>()
const query = ref(props.query)
const searchBox = ref<HTMLElement | null>(null)
const suggestionsOpen = ref(false)
const activeSuggestion = ref(-1)
let timer: ReturnType<typeof setTimeout> | undefined
let suppressNextSearch = false
watch(() => props.query, value => {
  if (value !== query.value) query.value = value
})
watch(query, value => {
  emit('update:query', value)
  clearTimeout(timer)
  if (suppressNextSearch) {
    suppressNextSearch = false
    return
  }
  timer = setTimeout(() => emit('search', value), 300)
})
watch(() => props.targets, targets => {
  activeSuggestion.value = targets.length ? 0 : -1
  if (searchBox.value?.contains(document.activeElement)) {
    suggestionsOpen.value = targets.length > 0
  }
})
onBeforeUnmount(() => clearTimeout(timer))
function addAlgorithm(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) emit('addAlgorithm', file)
}
function importPack(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) emit('importPack', file)
}
function selectTarget(target: TargetRecord): void {
  clearTimeout(timer)
  if (query.value !== target.part_number) {
    suppressNextSearch = true
    query.value = target.part_number
  }
  suggestionsOpen.value = false
  activeSuggestion.value = -1
  emit('select', target)
}
function onSearchInput(): void {
  suggestionsOpen.value = false
  activeSuggestion.value = -1
}
function onSearchFocus(): void {
  suggestionsOpen.value = props.targets.length > 0
}
function onSearchFocusOut(): void {
  setTimeout(() => {
    if (!searchBox.value?.contains(document.activeElement)) suggestionsOpen.value = false
  }, 0)
}
function onSearchKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    suggestionsOpen.value = false
    activeSuggestion.value = -1
    return
  }
  if (!props.targets.length) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    suggestionsOpen.value = true
    const delta = event.key === 'ArrowDown' ? 1 : -1
    const start = activeSuggestion.value < 0 ? (delta > 0 ? -1 : 0) : activeSuggestion.value
    activeSuggestion.value = (start + delta + props.targets.length) % props.targets.length
    return
  }
  if (event.key === 'Enter' && suggestionsOpen.value && activeSuggestion.value >= 0) {
    event.preventDefault()
    selectTarget(props.targets[activeSuggestion.value])
  }
}
function targetAvailability(target: TargetRecord): string {
  if (target.part_number.toLowerCase().startsWith('hpm')) return tr('内置 ROM API', 'Built-in ROM API')
  if (target.source === 'bundle' || target.source === 'builtin') return tr('内置可用', 'Built-in')
  if (target.installed) return tr('本地 Pack', 'Local Pack')
  return tr('可导入或联网下载', 'Import or download')
}
function hex(value: number): string { return `0x${value.toString(16).toUpperCase().padStart(8, '0')}` }
const visibleAlgorithms = computed<FlashAlgorithmRecord[]>(() => props.flashAlgorithms?.length ? props.flashAlgorithms : props.algorithms.map(algorithm => ({
  algorithm_id: algorithm.algorithm_id,
  target_part: algorithm.target_part,
  file_name: algorithm.file_name,
  flash_start: algorithm.flash_start,
  flash_size: algorithm.flash_size,
  default: false,
  source_kind: 'custom-flm',
  source_name: tr('用户 FLM', 'User FLM'),
})))
function algorithmSource(algorithm: FlashAlgorithmRecord): string {
  const kind = ({
    'installed-pack': tr('Pack FLM', 'Pack FLM'),
    'builtin-pack': tr('内置 Pack FLM', 'Built-in Pack FLM'),
    'daplink-builtin': tr('内置算法', 'Built-in algorithm'),
    'pyocd-builtin': tr('pyOCD 内置算法', 'pyOCD built-in algorithm'),
    'custom-flm': tr('自定义 FLM', 'Custom FLM'),
    'hpm-rom-api': 'HPM ROM API',
  } as Record<string, string>)[algorithm.source_kind] || algorithm.source_name
  return algorithm.source_name && algorithm.source_name !== kind && algorithm.source_kind !== 'hpm-rom-api'
    ? `${kind} · ${algorithm.source_name}`
    : kind
}
const phaseLabel = computed(() => ({
  preparing: tr('准备', 'Preparing'),
  downloading: tr('下载', 'Downloading'),
  refreshing: tr('安装并刷新', 'Installing and refreshing'),
}[props.phase] || tr('处理中', 'Processing')))
</script>

<template>
  <section class="target-panel">
    <div class="title-row"><h3>{{ tr('器件选择', 'Target Selection') }}</h3><span data-testid="pack-status" class="badge" :class="selectedPart && selectedInstalled ? 'ok' : ''">{{ selectedPart && selectedInstalled ? tr('已安装', 'Installed') : tr('未就绪', 'Not ready') }}</span></div>
    <div ref="searchBox" class="target-combobox" @focusout="onSearchFocusOut">
      <input v-model="query" data-testid="target-search" type="search" role="combobox" aria-autocomplete="list" aria-controls="target-suggestions" :aria-expanded="suggestionsOpen" :aria-activedescendant="suggestionsOpen && activeSuggestion >= 0 ? `target-option-${activeSuggestion}` : undefined" :placeholder="tr('搜索型号 / 厂商 / 系列', 'Search model / vendor / family / series')" :aria-label="tr('搜索器件', 'Search targets')" @input="onSearchInput" @focus="onSearchFocus" @keydown="onSearchKeydown">
      <div v-show="suggestionsOpen && targets.length" id="target-suggestions" class="target-list" role="listbox">
        <button v-for="(target, index) in targets" :id="`target-option-${index}`" :key="target.part_number" :data-testid="`target-${target.part_number}`" type="button" role="option" :aria-selected="selectedPart === target.part_number" :disabled="busy || algorithmBusy" :class="{ active: index === activeSuggestion || selectedPart === target.part_number }" @mouseenter="activeSuggestion = index" @click="selectTarget(target)">
          <strong>{{ target.part_number }}</strong><small>{{ target.vendor }} · {{ target.pack_id || tr('内置', 'Built-in') }}</small><span>{{ targetAvailability(target) }}</span>
        </button>
      </div>
    </div>
    <div v-if="busy" class="pack-progress"><progress :value="progress" max="1"/><span data-testid="pack-progress-label">{{ phaseLabel }} {{ Math.round(progress * 100) }}%</span><button data-testid="pack-cancel" :disabled="cancelPending" @click="emit('cancel')">{{ cancelPending ? tr('取消中…', 'Canceling…') : tr('取消', 'Cancel') }}</button></div>
    <p v-if="error" class="error">{{ error }}</p>
    <div class="pack-footer"><span>{{ tr('索引', 'Index') }} {{ status?.index_available ? tr('可用', 'available') : tr('不可用', 'unavailable') }} · {{ status?.target_count ?? 0 }} {{ tr('型号', 'targets') }}</span><div class="pack-actions"><label class="file-button" :class="{ disabled: busy || algorithmBusy }">{{ tr('导入 Pack', 'Import Pack') }}<input data-testid="pack-import-input" type="file" accept=".pack" :disabled="busy || algorithmBusy" @change="importPack"></label><button data-testid="pack-update-index" :disabled="busy || algorithmBusy" @click="emit('updateIndex')">{{ tr('联网更新', 'Update Online') }}</button></div></div>
    <div class="algorithm-heading"><span>{{ tr('烧录算法', 'Flash Algorithms') }}</span><label v-if="!algorithmNotRequired" class="file-button" :class="{ disabled: !canManageAlgorithms || algorithmBusy }">{{ tr('添加 FLM', 'Add FLM') }}<input data-testid="custom-flm-input" type="file" accept=".flm" :disabled="!canManageAlgorithms || algorithmBusy" @change="addAlgorithm"></label></div>
    <p v-if="algorithmNotRequired && !visibleAlgorithms.length" class="algorithm-not-required"><strong>HPM ROM API</strong><span>{{ tr('内置 ROM API · 无需 FLM', 'Built-in ROM API · No FLM required') }}</span></p>
    <div v-if="visibleAlgorithms.length" class="algorithm-list" data-testid="flash-algorithm-list">
      <div v-for="algorithm in visibleAlgorithms" :key="algorithm.algorithm_id" :data-testid="algorithm.source_kind === 'custom-flm' ? `custom-flm-${algorithm.algorithm_id}` : `flash-algorithm-${algorithm.algorithm_id}`" class="algorithm-row">
        <strong>{{ algorithm.file_name }}</strong><span>{{ algorithmSource(algorithm) }} · {{ hex(algorithm.flash_start) }}–{{ hex(algorithm.flash_start + algorithm.flash_size) }}</span><button v-if="algorithm.source_kind === 'custom-flm'" :disabled="algorithmBusy" @click="emit('removeAlgorithm', algorithm.algorithm_id)">{{ tr('移除', 'Remove') }}</button>
      </div>
    </div>
    <p v-else-if="!algorithmNotRequired" class="algorithm-empty">{{ algorithmBusy ? tr('正在读取烧录算法…', 'Loading flash algorithms…') : tr('当前器件未找到可用烧录算法', 'No flash algorithm found for this target') }}</p>
    <p v-if="algorithmError" class="error">{{ algorithmError }}</p>
  </section>
</template>

<style scoped>
.target-panel{padding:14px}.title-row,.pack-footer,.pack-progress,.algorithm-heading,.pack-actions{display:flex;align-items:center;justify-content:space-between;gap:8px}h3{margin:0;font-size:13px}input{box-sizing:border-box;width:100%;margin:10px 0;padding:8px;border:1px solid var(--of-border);border-radius:5px;background:var(--of-input);color:var(--of-text)}.badge{padding:2px 7px;border-radius:10px;background:var(--of-danger-bg);color:var(--of-danger);font-size:10px}.badge.ok{background:var(--of-ok-bg);color:var(--of-ok)}.target-combobox{position:relative}.target-list{position:absolute;z-index:20;top:calc(100% - 8px);left:0;right:0;max-height:240px;overflow:auto;display:grid;gap:2px;padding:4px;border:1px solid var(--of-accent);border-radius:5px;background:var(--of-surface);box-shadow:0 8px 20px rgba(0,0,0,.3)}.target-list button{display:grid;grid-template-columns:1fr auto;text-align:left;padding:8px;border:1px solid transparent;border-radius:4px;background:var(--of-input);color:var(--of-text)}.target-list button.active{border-color:var(--of-accent);background:rgba(88,166,214,.16)}small{grid-column:1 / -1;color:var(--of-muted)}.target-list span{font-size:10px;color:var(--of-muted)}button,.file-button{border:1px solid var(--of-border);border-radius:4px;background:var(--of-input);color:var(--of-text);padding:5px 8px}.pack-progress{margin-top:8px;font-size:10px}.pack-progress progress{flex:1}.pack-footer{margin-top:10px;color:var(--of-muted);font-size:10px}.pack-actions{justify-content:flex-end}.algorithm-heading{margin-top:14px;padding-top:12px;border-top:1px solid var(--of-border);font-size:11px}.algorithm-not-required{display:flex;justify-content:space-between;color:var(--of-ok);font-size:10px}.file-button{position:relative;overflow:hidden;cursor:pointer}.file-button input{position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0;cursor:pointer}.file-button.disabled{opacity:.45;cursor:not-allowed}.algorithm-list{display:grid;gap:5px;margin-top:7px}.algorithm-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 6px;padding:6px 0;border-bottom:1px solid var(--of-border);font-size:10px}.algorithm-row strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.algorithm-row span{grid-column:1;color:var(--of-muted)}.algorithm-row button{grid-column:2;grid-row:1 / span 2}.algorithm-empty{margin:7px 0 0;color:var(--of-muted);font-size:10px}.error{color:var(--of-danger);font-size:11px}
</style>
