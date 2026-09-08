<template>
  <aside class="peripheral-panel" data-testid="peripheral-panel">
    <div ref="searchBox" class="chip-search" @focusout="closeSuggestions">
      <label for="peripheral-chip-input">{{ tr('芯片型号', 'Chip model') }}</label>
      <input id="peripheral-chip-input" v-model="chipQuery" class="form-input" data-testid="peripheral-chip-search" role="combobox" aria-autocomplete="list" aria-controls="peripheral-chip-suggestions" :aria-expanded="suggestionsOpen" :aria-activedescendant="suggestionsOpen && activeSuggestion >= 0 ? `peripheral-chip-option-${activeSuggestion}` : undefined" :placeholder="tr('输入型号，如 STM32F103RE', 'Type a model, e.g. STM32F103RE')" @input="searchInput" @focus="suggestionsOpen = true" @keydown="searchKeydown" />
      <div v-if="suggestionsOpen && targets.length" id="peripheral-chip-suggestions" role="listbox" class="chip-suggestions">
        <button v-for="(target, index) in targets" :id="`peripheral-chip-option-${index}`" :key="target.id" type="button" role="option" :aria-selected="index === activeSuggestion" class="chip-suggestion" @mousedown.prevent @click="chooseTarget(target)"><strong>{{ target.target }}</strong><small>{{ target.pack }}</small></button>
      </div>
    </div>
    <select v-model="targetId" class="form-input" data-testid="peripheral-svd" :aria-label="tr('SVD 文件', 'SVD file')">
      <option value="">{{ tr('匹配的 SVD 文件', 'Matching SVD file') }}</option>
      <option v-for="target in matchingSvds" :key="target.id" :value="target.id" :title="target.pack">{{ target.svd.split(/[\\/]/).pop() }}</option>
    </select>
    <button class="btn btn-primary" data-testid="peripheral-load" :disabled="!deviceConnected || !targetId || busy" @click="loadChip">{{ tr('加载外设', 'Load peripherals') }}</button>
    <p v-if="selection" data-testid="peripheral-source">{{ selection.target }} · {{ selection.pack }} · {{ selection.svd }} · {{ items.length }} {{ tr('项（只读）', 'items (read only)') }}</p>
    <p>{{ tr('GPIO 引脚读取 IDR 电平；ODR 为输出锁存值。采样可能漏掉短脉冲，GPIO 时钟须由程序开启。', 'GPIO pins sample IDR levels; ODR is the output latch. Short pulses may be missed. Firmware must enable the GPIO clock.') }}</p>
    <select v-model="group" class="form-input" data-testid="peripheral-group"><option value="">{{ tr('所有外设', 'All peripherals') }}</option><option v-for="name in groups" :key="name">{{ name }}</option></select>
    <input v-model="query" class="form-input" data-testid="peripheral-search" :placeholder="tr('搜索，如 GPIOB.12', 'Search, e.g. GPIOB.12')" />
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!items.length">{{ tr('选择实际芯片型号，加载 Pack 的 SVD。无需 AXF。', 'Select the actual chip to load its Pack SVD. No AXF needed.') }}</p>
    <p>{{ tr('已过滤 SVD 标注的只写和读取副作用寄存器；未标注的行为仍以芯片手册为准。', 'Write-only registers and SVD-declared read side effects are excluded; consult the chip manual for undocumented behavior.') }}</p>
    <div class="peripheral-list">
      <label v-for="item in filtered" :key="item.name" :title="`${item.register} @ ${item.address}`">
        <input type="checkbox" :data-testid="`peripheral-${item.name}`" :checked="selected.has(item.name)" :disabled="!deviceConnected || busy" @change="toggle(item.name, $event)" />
        <code>{{ item.name }}</code>
        <output>{{ latestValues[item.name] ?? '—' }}</output>
      </label>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { API_BASE } from '../../lib/runtimeEndpoint'
import { tr } from '../../composables/useLanguage'

const props = defineProps<{ deviceConnected: boolean; latestValues: Record<string, number | boolean> }>()
type Peripheral = { name: string; register: string; address: string }
const items = ref<Peripheral[]>([])
const selected = ref(new Set<string>())
const query = ref('')
const error = ref('')
const busy = ref(false)
type Target = { id: string; target: string; pack: string; svd: string }
const targets = ref<Target[]>([])
const targetId = ref('')
const chipQuery = ref('')
const chosenModel = ref('')
const matchingSvds = computed(() => targets.value.filter(target => target.target === chosenModel.value))
const searchBox = ref<HTMLElement | null>(null)
const suggestionsOpen = ref(false)
const activeSuggestion = ref(-1)
let searchTimer: ReturnType<typeof setTimeout> | undefined
let blurTimer: ReturnType<typeof setTimeout> | undefined
const selection = ref<Target | null>(null)
const group = ref('')
const groups = computed(() => [...new Set(items.value.map(item => item.name.split('.')[0]!))].sort())
let targetRequest = 0
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined
const filtered = computed(() => items.value.filter(item => (!group.value || item.name.startsWith(group.value + '.')) && item.name.toLowerCase().includes(query.value.trim().toLowerCase())).slice(0, 200))

async function loadTargets() {
  const epoch = ++targetRequest
  try {
    const payload = await request('peripherals/targets?q=' + encodeURIComponent(chipQuery.value))
    if (epoch === targetRequest) {
      targets.value = payload.targets ?? []
      activeSuggestion.value = targets.value.length ? 0 : -1
    }
  } catch (cause) { if (epoch === targetRequest) error.value = String(cause) }
}
function applyCatalog(payload: any) {
  items.value = payload.items ?? []
  selection.value = payload.selection ?? null
}
async function loadChip() {
  busy.value = true
  error.value = ''
  const epoch = generation
  try {
    const payload = await request('peripherals/select', { target_id: targetId.value })
    if (epoch !== generation) return
    applyCatalog(payload)
    selected.value = new Set()
    group.value = groups.value.includes('GPIOB') ? 'GPIOB' : groups.value[0] ?? ''
  } catch (cause) { if (epoch === generation) error.value = String(cause) }
  finally { busy.value = false }
}
function chooseTarget(target: Target) {
  clearTimeout(searchTimer)
  targetRequest++
  chipQuery.value = target.target
  chosenModel.value = target.target
  targetId.value = target.id
  suggestionsOpen.value = false
}
function searchInput() {
  chosenModel.value = ''
  targetId.value = ''
  targetRequest++
  suggestionsOpen.value = true
  clearTimeout(searchTimer)
  searchTimer = setTimeout(loadTargets, 150)
}
function closeSuggestions() {
  clearTimeout(blurTimer)
  blurTimer = setTimeout(() => {
    if (!searchBox.value?.contains(document.activeElement)) suggestionsOpen.value = false
  }, 0)
}
function searchKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { suggestionsOpen.value = false; return }
  if (!targets.value.length) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    suggestionsOpen.value = true
    activeSuggestion.value = (activeSuggestion.value + (event.key === 'ArrowDown' ? 1 : -1) + targets.value.length) % targets.value.length
  } else if (event.key === 'Enter' && suggestionsOpen.value && activeSuggestion.value >= 0) {
    event.preventDefault()
    chooseTarget(targets.value[activeSuggestion.value]!)
  }
}
onMounted(loadTargets)

async function request(path: string, body?: object) {
  const response = await fetch(`${API_BASE}/api/dash/superwatch/${path}`, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : undefined)
  const payload = await response.json()
  if (!response.ok || payload.error || payload.item?.error) throw new Error(payload.error || payload.item?.error || payload.detail?.message || payload.detail || response.statusText)
  return payload
}

async function refresh(epoch: number) {
  if (!props.deviceConnected) return
  try {
    const watches = await request('items')
    if (epoch !== generation || busy.value) return
    const available = new Set(items.value.map(item => item.name))
    selected.value = new Set((watches.items ?? []).map((item: { name: string }) => item.name).filter((name: string) => available.has(name)))
  } catch (cause) {
    if (epoch === generation) error.value = String(cause)
  } finally {
    if (epoch === generation) timer = setTimeout(() => void refresh(epoch), 2000)
  }
}

async function toggle(name: string, event: Event) {
  ;(event.target as HTMLInputElement).checked = selected.value.has(name)
  busy.value = true
  error.value = ''
  const epoch = generation
  try {
    const removing = selected.value.has(name)
    await request(removing ? 'remove' : 'add', { name })
    if (epoch !== generation) return
    const next = new Set(selected.value)
    if (removing) next.delete(name)
    else next.add(name)
    selected.value = next
  } catch (cause) {
    if (epoch === generation) error.value = String(cause)
  } finally {
    busy.value = false
    await nextTick()
    ;(event.target as HTMLInputElement).checked = selected.value.has(name)
  }
}

watch(() => props.deviceConnected, async connected => {
  const epoch = ++generation
  clearTimeout(timer)
  items.value = []
  selection.value = null
  selected.value = new Set()
  error.value = ''
  if (!connected) return
  try {
    const payload = await request('peripherals')
    if (epoch !== generation) return
    applyCatalog(payload)
    await refresh(epoch)
  } catch (cause) {
    if (epoch === generation) error.value = String(cause)
  }
}, { immediate: true })

onUnmounted(() => { generation++; targetRequest++; clearTimeout(timer); clearTimeout(searchTimer); clearTimeout(blurTimer) })
</script>

<style scoped>
.peripheral-panel { flex: 1; min-height: 0; overflow: auto; padding: 8px 10px; }
.chip-search { position: relative; }
.chip-suggestions { position: absolute; z-index: 10; top: 100%; left: 0; right: 0; max-height: 220px; overflow: auto; background: var(--surface); border: 1px solid var(--border); box-shadow: 0 4px 12px #0002; }
.chip-suggestion { display: flex; flex-direction: column; width: 100%; padding: 8px; background: var(--surface); color: var(--fg); border: 0; text-align: left; cursor: pointer; }
.chip-suggestion[aria-selected="true"], .chip-suggestion:hover { background: var(--bg); color: var(--accent); }
.chip-suggestion small { color: var(--muted); font-size: 10px; }
summary { cursor: pointer; font-weight: 600; font-size: 12px; }
summary span { color: var(--accent); }
p { font-size: 11px; color: var(--muted); margin: 7px 0; line-height: 1.5; }
.form-input { width: 100%; box-sizing: border-box; margin-top: 8px; }
.peripheral-list { padding-top: 8px; }
label { display: flex; align-items: center; gap: 6px; padding: 5px 0; font-size: 12px; }
output { margin-left: auto; color: var(--accent); font-family: var(--font-mono); }
</style>
