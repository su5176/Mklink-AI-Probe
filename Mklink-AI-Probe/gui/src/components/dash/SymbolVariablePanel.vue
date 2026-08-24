<template>
  <aside class="symbol-panel">
    <div class="panel-toolbar">
      <input
        v-model="query"
        class="form-input"
        data-testid="variable-search"
        :placeholder="tr('搜索变量', 'Search variables')"
      />
      <button
        class="icon-button"
        type="button"
        :title="tr('手动添加变量', 'Add variable manually')"
        :aria-label="tr('手动添加变量', 'Add variable manually')"
        data-testid="show-manual-add"
        @click="manualAddOpen = !manualAddOpen"
      >
        <Plus :size="16" aria-hidden="true" />
      </button>
      <button
        class="icon-button"
        type="button"
        :title="tr('粘贴 C 结构定义', 'Paste C structure definition')"
        :aria-label="tr('粘贴 C 结构定义', 'Paste C structure definition')"
        data-testid="show-c-layout"
        @click="openCLayout()"
      >
        <Code2 :size="16" aria-hidden="true" />
      </button>
      <button
        class="icon-button"
        type="button"
        :title="tr('重新解析符号', 'Reparse symbols')"
        :aria-label="tr('重新解析符号', 'Reparse symbols')"
        :disabled="catalog.reparsing.value"
        data-testid="reparse-symbols"
        @click="reparseSymbols"
      >
        <RefreshCw :size="16" aria-hidden="true" />
      </button>
    </div>

    <form v-if="manualAddOpen" class="manual-add-row" @submit.prevent="addManualVariable">
      <input
        v-model="manualPath"
        class="form-input"
        data-testid="manual-variable-path"
        :placeholder="tr('变量或成员路径', 'Variable or member path')"
        autocomplete="off"
      />
      <button
        class="icon-button"
        type="submit"
        :title="tr('添加到 SuperWatch', 'Add to SuperWatch')"
        :aria-label="tr('添加到 SuperWatch', 'Add to SuperWatch')"
        data-testid="add-manual-variable"
        :disabled="manualAdding || !manualPath.trim()"
      >
        <Plus :size="16" aria-hidden="true" />
      </button>
    </form>

    <div class="panel-filters">
      <label>
        <input v-model="selectedOnly" type="checkbox" data-testid="selected-only" />
        {{ tr('仅已选', 'Selected Only') }}
      </label>
      <span>{{ tr(`已选 ${selected.size}`, `${selected.size} selected`) }}</span>
    </div>

    <div v-if="catalog.stale.value" class="stale-banner">{{ tr('AXF 已变化，请重新解析', 'AXF changed. Reparse symbols.') }}</div>
    <SetupHint
      v-if="!deviceConnected"
      kind="device"
      :message="tr('SuperWatch 读取变量前需要连接 MKLink 设备。', 'Connect the MKLink device before reading variables with SuperWatch.')"
      :primary-label="tr('连接设备', 'Connect Device')"
      :busy="connecting"
      @primary="quickConnect"
    />
    <SetupHint
      v-else-if="symbolError || catalog.error.value"
      kind="error"
      :message="tr('符号文件解析失败：', 'Symbol parsing failed: ') + (symbolError || catalog.error.value)"
      :primary-label="tr('重新选择', 'Choose Another File')"
      :secondary-label="hasSymbolSource ? tr('重试解析', 'Retry Parsing') : ''"
      :busy="loadingSymbols"
      @primary="loadSymbolFile"
      @secondary="parseSelectedSymbols"
    />
    <SetupHint
      v-else-if="!symbolLoaded"
      kind="symbols"
      :message="hasSymbolSource ? tr('已选择 AXF / ELF，解析后即可浏览变量。', 'An AXF / ELF file is selected. Parse it to browse variables.') : tr('SuperWatch 需要 AXF / ELF 中的变量和类型信息。', 'SuperWatch needs variable and type information from an AXF / ELF file.')"
      :primary-label="hasSymbolSource ? tr('解析已选文件', 'Parse Selected File') : tr('加载 AXF / ELF', 'Load AXF / ELF')"
      :busy="loadingSymbols"
      @primary="hasSymbolSource ? parseSelectedSymbols() : loadSymbolFile()"
    />
    <div v-else-if="catalog.loading.value" class="empty-state">{{ tr('正在加载符号...', 'Loading symbols...') }}</div>
    <div v-else class="variable-groups">
      <h3 class="variable-root-heading">{{ tr('全局变量', 'Global Variables') }}</h3>
      <template v-for="row in rows" :key="row.node.key">
        <div
          v-if="row.node.kind === 'branch' || row.node.kind === 'range'"
          class="branch-row"
          :title="row.node.key"
          @click="toggleBranch(row.node)"
        >
          <button
            class="branch-toggle"
            type="button"
            :data-testid="`branch-${row.node.key}`"
            :style="{ paddingLeft: rowIndent(row.depth) }"
            @click.stop="toggleBranch(row.node)"
          >
            <LoaderCircle v-if="catalog.browseLoading.value.has(row.node.key)" class="branch-spinner" :size="15" aria-hidden="true" />
            <ChevronDown v-else-if="row.expanded" :size="15" aria-hidden="true" />
            <ChevronRight v-else :size="15" aria-hidden="true" />
            <span class="branch-name">{{ row.node.label }}</span>
            <span v-if="row.node.childCount !== null" class="branch-count">
              {{ row.node.kind === 'range' ? row.node.childCount : `${row.selectedLeafCount} / ${row.node.childCount}` }}
            </span>
          </button>
          <button
            v-if="isArraySnapshotNode(row.node)"
            class="snapshot-button"
            :class="{ active: snapshotPath === row.node.key }"
            type="button"
            :data-testid="`snapshot-${row.node.key}`"
            :disabled="snapshotBusy === row.node.key"
            :title="snapshotPath === row.node.key ? tr('关闭数组快照曲线', 'Close array snapshot curve') : tr('设置数组快照范围', 'Configure array snapshot range')"
            :aria-label="snapshotPath === row.node.key ? tr(`关闭 ${row.node.key} 快照曲线`, `Close ${row.node.key} snapshot curve`) : tr(`设置 ${row.node.key} 快照范围`, `Configure ${row.node.key} snapshot range`)"
            :aria-pressed="snapshotPath === row.node.key"
            @click.stop="toggleArraySnapshot(row.node)"
          >
            <Activity :size="15" aria-hidden="true" />
          </button>
          <button
            v-if="snapshotPath === row.node.key"
            class="visibility-button"
            :class="{ hidden: hiddenChannels?.has(row.node.key) }"
            type="button"
            :data-testid="`visibility-${row.node.key}`"
            :aria-label="hiddenChannels?.has(row.node.key) ? tr(`显示 ${row.node.key} 波形`, `Show ${row.node.key} waveform`) : tr(`隐藏 ${row.node.key} 波形`, `Hide ${row.node.key} waveform`)"
            :aria-pressed="!hiddenChannels?.has(row.node.key)"
            :title="hiddenChannels?.has(row.node.key) ? tr('显示波形', 'Show waveform') : tr('隐藏波形', 'Hide waveform')"
            @click.stop="toggleVisibility(row.node.key)"
          >
            <EyeOff v-if="hiddenChannels?.has(row.node.key)" :size="15" aria-hidden="true" />
            <Eye v-else :size="15" aria-hidden="true" />
          </button>
        </div>
        <button
          v-else-if="row.node.kind === 'container' && row.node.container"
          class="container-row"
          type="button"
          :data-testid="`container-${row.node.container.path}`"
          :title="row.node.container.path"
          :style="{ paddingLeft: rowIndent(row.depth) }"
          @click="openCLayout(row.node.container.path)"
        >
          <Code2 :size="15" aria-hidden="true" />
          <span class="branch-name">{{ row.node.label }}</span>
          <span class="container-type">{{ row.node.container.type_name }}</span>
          <span class="container-state">{{ tr('待定义', 'Needs definition') }}</span>
        </button>
        <div
          v-else-if="row.node.descriptor"
          class="variable-row"
          :class="{ selected: selected.has(row.node.descriptor.path) }"
          :data-testid="`leaf-${row.node.descriptor.path}`"
        >
          <div class="variable-main" :style="{ paddingLeft: rowIndent(row.depth) }">
            <input
              type="checkbox"
              :checked="selected.has(row.node.descriptor.path)"
              :data-testid="`toggle-${row.node.descriptor.path}`"
              :disabled="selectionBusy.has(row.node.descriptor.path)"
              @change="toggleSelection(row.node.descriptor, $event)"
            />
            <span class="visibility-slot">
              <button
                v-if="selected.has(row.node.descriptor.path)"
                class="visibility-button"
                type="button"
                :class="{ hidden: hiddenChannels?.has(row.node.descriptor.path) }"
                :data-testid="`visibility-${row.node.descriptor.path}`"
                :aria-label="hiddenChannels?.has(row.node.descriptor.path) ? tr(`显示 ${row.node.descriptor.path} 波形`, `Show ${row.node.descriptor.path} waveform`) : tr(`隐藏 ${row.node.descriptor.path} 波形`, `Hide ${row.node.descriptor.path} waveform`)"
                :aria-pressed="!hiddenChannels?.has(row.node.descriptor.path)"
                :title="hiddenChannels?.has(row.node.descriptor.path) ? tr('显示波形', 'Show waveform') : tr('隐藏波形', 'Hide waveform')"
                @click.stop="toggleVisibility(row.node.descriptor.path)"
              >
                <EyeOff v-if="hiddenChannels?.has(row.node.descriptor.path)" :size="15" aria-hidden="true" />
                <Eye v-else :size="15" aria-hidden="true" />
              </button>
            </span>
            <button
              class="variable-name"
              type="button"
              :title="row.node.descriptor.path"
              @click="beginEdit(row.node.descriptor)"
            >
              {{ row.node.label }}
            </button>
            <span class="variable-type">{{ row.node.descriptor.type_name }}</span>
            <span :data-testid="`latest-${row.node.descriptor.path}`" class="variable-value">
              {{ formatValue(latestValues[row.node.descriptor.path]) }}
            </span>
            <button
              class="edit-button"
              type="button"
              :data-testid="`edit-${row.node.descriptor.path}`"
              :disabled="catalog.stale.value || !row.node.descriptor.writable"
              :title="tr('设置变量', 'Set variable')"
              @click="beginEdit(row.node.descriptor)"
            >
              {{ tr('编辑', 'Edit') }}
            </button>
          </div>

          <div v-if="editing === row.node.descriptor.path" class="write-editor">
            <select
              v-if="row.node.descriptor.scalar_kind === 'bool'"
              v-model="editValues[row.node.descriptor.path]"
              :data-testid="`write-input-${row.node.descriptor.path}`"
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
            <select
              v-else-if="row.node.descriptor.scalar_kind === 'enum'"
              v-model="editValues[row.node.descriptor.path]"
              :data-testid="`write-input-${row.node.descriptor.path}`"
            >
              <option v-for="(_value, label) in row.node.descriptor.enum_values" :key="label" :value="label">
                {{ label }}
              </option>
            </select>
            <input
              v-else
              v-model="editValues[row.node.descriptor.path]"
              class="form-input"
              :data-testid="`write-input-${row.node.descriptor.path}`"
              inputmode="decimal"
            />
            <button
              type="button"
              class="btn btn-primary"
              :data-testid="`write-${row.node.descriptor.path}`"
              :disabled="writing.has(row.node.descriptor.path)"
              @click="writeValue(row.node.descriptor)"
            >
              {{ writing.has(row.node.descriptor.path) ? tr('写入中', 'Writing') : tr('写入', 'Write') }}
            </button>
            <button type="button" class="btn btn-secondary" @click="editing = null">{{ tr('取消', 'Cancel') }}</button>
          </div>
          <div
            v-if="writeSuccess[row.node.descriptor.path] !== undefined"
            class="write-success"
            :data-testid="`write-ok-${row.node.descriptor.path}`"
          >
            {{ tr('已验证:', 'Verified:') }} {{ formatValue(writeSuccess[row.node.descriptor.path]) }}
          </div>
        </div>
      </template>
      <div v-if="rows.length === 0" class="empty-state">{{ tr('无匹配变量', 'No matching variables') }}</div>
    </div>

    <div v-if="cLayoutOpen" class="modal-overlay" data-testid="c-layout-modal" @click.self="closeCLayout">
      <section class="layout-modal" role="dialog" aria-modal="true" aria-labelledby="c-layout-title">
        <header class="layout-modal-header">
          <h3 id="c-layout-title"><Code2 :size="17" aria-hidden="true" />{{ tr('应用 C 结构定义', 'Apply C Structure Definition') }}</h3>
          <button class="icon-button" type="button" :title="tr('关闭', 'Close')" :aria-label="tr('关闭', 'Close')" @click="closeCLayout">
            <X :size="16" aria-hidden="true" />
          </button>
        </header>
        <label class="layout-field">
          <span>{{ tr('变量', 'Variable') }}</span>
          <input
            v-model="cLayoutVariable"
            class="form-input"
            data-testid="c-layout-variable"
            autocomplete="off"
            placeholder="data_save"
          />
        </label>
        <label class="layout-field">
          <span>{{ tr('对齐', 'Alignment') }}</span>
          <select v-model="cLayoutPack" class="form-input" data-testid="c-layout-pack">
            <option value="">{{ tr('自动', 'Auto') }}</option>
            <option value="1">pack(1)</option>
            <option value="2">pack(2)</option>
            <option value="4">pack(4)</option>
            <option value="8">pack(8)</option>
          </select>
        </label>
        <label class="layout-field layout-definition">
          <span>{{ tr('C 定义', 'C Definition') }}</span>
          <textarea
            v-model="cLayoutDefinition"
            class="form-input"
            data-testid="c-layout-definition"
            spellcheck="false"
            placeholder="typedef struct { ... } TypeName;"
          />
        </label>
        <footer class="layout-modal-actions">
          <button type="button" class="btn btn-secondary" @click="closeCLayout">{{ tr('取消', 'Cancel') }}</button>
          <button
            type="button"
            class="btn btn-primary"
            data-testid="apply-c-layout"
            :disabled="catalog.applyingLayout.value || !cLayoutVariable.trim() || !cLayoutDefinition.trim()"
            @click="applyCLayout"
          >
            {{ catalog.applyingLayout.value ? tr('解析中', 'Parsing') : tr('应用', 'Apply') }}
          </button>
        </footer>
      </section>
    </div>

    <div v-if="snapshotDialogNode" class="modal-overlay" data-testid="array-snapshot-modal" @click.self="closeArraySnapshotDialog">
      <section class="layout-modal array-snapshot-modal" role="dialog" aria-modal="true" aria-labelledby="array-snapshot-title">
        <header class="layout-modal-header">
          <h3 id="array-snapshot-title"><Activity :size="17" aria-hidden="true" />{{ tr('设置数组快照', 'Configure Array Snapshot') }}</h3>
          <button class="icon-button" type="button" :title="tr('关闭', 'Close')" :aria-label="tr('关闭', 'Close')" @click="closeArraySnapshotDialog">
            <X :size="16" aria-hidden="true" />
          </button>
        </header>
        <p class="snapshot-modal-help">{{ snapshotDialogNode.key }} · {{ tr(`数组长度 ${arrayLength(snapshotDialogNode)}`, `Array length ${arrayLength(snapshotDialogNode)}`) }}</p>
        <label class="layout-field">
          <span>{{ tr('起始索引', 'Start index') }}</span>
          <input v-model="snapshotStartIndex" class="form-input" data-testid="array-snapshot-start" inputmode="numeric" />
        </label>
        <label class="layout-field">
          <span>{{ tr('读取数量', 'Read count') }}</span>
          <input v-model="snapshotCount" class="form-input" data-testid="array-snapshot-count" inputmode="numeric" />
        </label>
        <p v-if="snapshotRangeError" class="snapshot-range-error" role="alert">{{ snapshotRangeError }}</p>
        <footer class="layout-modal-actions">
          <button type="button" class="btn btn-secondary" @click="closeArraySnapshotDialog">{{ tr('取消', 'Cancel') }}</button>
          <button type="button" class="btn btn-primary" data-testid="array-snapshot-confirm" :disabled="snapshotBusy !== null" @click="confirmArraySnapshot">
            {{ snapshotBusy ? tr('读取中', 'Starting') : tr('开始快照', 'Start Snapshot') }}
          </button>
        </footer>
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { Activity, ChevronDown, ChevronRight, Code2, Eye, EyeOff, LoaderCircle, Plus, RefreshCw, X } from '@lucide/vue'
import { useSymbolCatalog } from '../../composables/useSymbolCatalog'
import { useToast } from '../../composables/useToast'
import { useDashboardSetup } from '../../composables/useDashboardSetup'
import { buildBrowseTree, buildSymbolTree, collectBranchKeys, visibleSymbolRows } from '../../lib/symbolTree'
import type { SymbolDescriptor } from '../../types/mklink'
import type { SymbolTreeNode } from '../../lib/symbolTree'
import { tr } from '../../composables/useLanguage'
import SetupHint from './SetupHint.vue'
import { API_BASE } from '../../lib/runtimeEndpoint'

const props = withDefaults(defineProps<{
  deviceConnected: boolean
  symbolLoaded?: boolean
  symbolError?: string
  latestValues: Record<string, number | boolean>
  hiddenChannels?: ReadonlySet<string>
  snapshotPath?: string | null
}>(), {
  symbolLoaded: true,
  symbolError: '',
  snapshotPath: null,
})

const emit = defineEmits<{
  'visibility-change': [path: string, visible: boolean]
  'selection-removed': [path: string]
  'snapshot-change': [path: string | null]
}>()

const catalog = useSymbolCatalog()
const toast = useToast()
const {
  connecting,
  loadingSymbols,
  hasSymbolSource,
  quickConnect,
  loadSymbolFile,
  parseSelectedSymbols,
} = useDashboardSetup()
const query = ref('')
const manualAddOpen = ref(false)
const manualPath = ref('')
const manualAdding = ref(false)
const cLayoutOpen = ref(false)
const cLayoutVariable = ref('')
const cLayoutDefinition = ref('')
const cLayoutPack = ref('')
const selectedOnly = ref(false)
const selected = shallowRef(new Set<string>())
const selectionBusy = shallowRef(new Set<string>())
const writing = shallowRef(new Set<string>())
const editing = ref<string | null>(null)
const editValues = reactive<Record<string, string>>({})
const writeSuccess = reactive<Record<string, number | boolean | undefined>>({})
const expanded = shallowRef(new Set<string>())
const searchItems = shallowRef<SymbolDescriptor[]>([])
const selectedDescriptors = shallowRef(new Map<string, SymbolDescriptor>())
const snapshotBusy = ref<string | null>(null)
const snapshotDialogNode = ref<SymbolTreeNode | null>(null)
const snapshotStartIndex = ref('0')
const snapshotCount = ref('128')
let searchExpansionSnapshot: Set<string> | null = null
let searchRequest = 0

const tree = computed(() => {
  if (query.value.trim()) {
    return buildSymbolTree(searchItems.value, catalog.containers.value)
  }
  if (selectedOnly.value) {
    return buildSymbolTree([...selectedDescriptors.value.values()])
  }
  return buildBrowseTree(catalog.browseRoots.value, catalog.browseChildren.value)
})
const rows = computed(() => visibleSymbolRows(tree.value, {
  expanded: expanded.value,
  selected: selected.value,
  query: query.value,
  selectedOnly: selectedOnly.value,
}))

async function request(path: string, options?: RequestInit): Promise<any> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload?.detail
    throw new Error(typeof detail === 'string' ? detail : detail?.message || response.statusText)
  }
  return payload
}

async function loadWorkspace(): Promise<void> {
  if (!props.deviceConnected || !props.symbolLoaded) return
  try {
    await catalog.ensureLoaded()
    const response = await request('/api/dash/superwatch/items')
    selected.value = new Set(
      Array.isArray(response.items) ? response.items.map((item: { name: string }) => item.name) : [],
    )
    await refreshSelectedDescriptors()
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  }
}

function isArraySnapshotNode(node: SymbolTreeNode): boolean {
  if (node.kind !== 'branch' || node.browse?.snapshot_eligible !== true) return false
  return Array.isArray(node.browse.array_dimensions) && node.browse.array_dimensions.length === 1
}

function arrayLength(node: SymbolTreeNode): number {
  return node.browse?.array_dimensions?.[0] ?? node.childCount ?? 0
}

const snapshotRangeError = computed(() => {
  if (!snapshotDialogNode.value) return ''
  const total = arrayLength(snapshotDialogNode.value)
  const start = Number(snapshotStartIndex.value)
  const count = Number(snapshotCount.value)
  if (!Number.isInteger(start) || start < 0 || start >= total) {
    return tr(`起始索引必须在 0 到 ${Math.max(0, total - 1)} 之间。`, `Start index must be between 0 and ${Math.max(0, total - 1)}.`)
  }
  if (!Number.isInteger(count) || count < 1 || count > 4096) {
    return tr('读取数量必须是 1 到 4096 之间的整数。', 'Read count must be an integer from 1 to 4096.')
  }
  if (start + count > total) {
    return tr(`读取范围不能超过数组长度 ${total}。`, `The range cannot exceed array length ${total}.`)
  }
  return ''
})

function openArraySnapshotDialog(node: SymbolTreeNode): void {
  const total = arrayLength(node)
  snapshotDialogNode.value = node
  snapshotStartIndex.value = '0'
  snapshotCount.value = String(Math.min(128, total))
}

function closeArraySnapshotDialog(): void {
  if (snapshotBusy.value) return
  snapshotDialogNode.value = null
}

async function toggleArraySnapshot(node: SymbolTreeNode): Promise<void> {
  if (props.snapshotPath === node.key) {
    snapshotBusy.value = node.key
    try {
      await request('/api/dash/superwatch/array-snapshot/clear', { method: 'POST' })
      emit('snapshot-change', null)
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause))
    } finally {
      snapshotBusy.value = null
    }
    return
  }
  openArraySnapshotDialog(node)
}

async function confirmArraySnapshot(): Promise<void> {
  const node = snapshotDialogNode.value
  if (!node || snapshotRangeError.value) return
  snapshotBusy.value = node.key
  try {
    await request('/api/dash/superwatch/array-snapshot/select', {
      method: 'POST',
      body: JSON.stringify({
        name: node.key,
        start_index: Number(snapshotStartIndex.value),
        count: Number(snapshotCount.value),
      }),
    })
    emit('snapshot-change', node.key)
    snapshotDialogNode.value = null
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  } finally {
    snapshotBusy.value = null
  }
}

function withSet(source: Set<string>, path: string, enabled: boolean): Set<string> {
  const next = new Set(source)
  if (enabled) next.add(path)
  else next.delete(path)
  return next
}

async function toggleBranch(node: SymbolTreeNode): Promise<void> {
  if (query.value.trim() || selectedOnly.value) return
  if (expanded.value.has(node.key)) {
    expanded.value = withSet(expanded.value, node.key, false)
    return
  }
  if (node.browse) {
    try {
      await catalog.loadBrowseChildren(node.browse)
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause))
      return
    }
  }
  expanded.value = withSet(expanded.value, node.key, true)
}

function rowIndent(depth: number): string {
  return `${8 + depth * 16}px`
}

async function toggleSelection(symbol: SymbolDescriptor, event: Event): Promise<void> {
  const path = symbol.path
  const checked = (event.target as HTMLInputElement).checked
  selectionBusy.value = withSet(selectionBusy.value, path, true)
  try {
    const action = checked ? 'add' : 'remove'
    await request(`/api/dash/superwatch/${action}`, {
      method: 'POST',
      body: JSON.stringify({ name: path }),
    })
    selected.value = withSet(selected.value, path, checked)
    const descriptors = new Map(selectedDescriptors.value)
    if (checked) descriptors.set(path, symbol)
    else descriptors.delete(path)
    selectedDescriptors.value = descriptors
    if (!checked) emit('selection-removed', path)
  } catch (cause) {
    ;(event.target as HTMLInputElement).checked = !checked
    toast.error(cause instanceof Error ? cause.message : String(cause))
  } finally {
    selectionBusy.value = withSet(selectionBusy.value, path, false)
  }
}

async function addManualVariable(): Promise<void> {
  const path = manualPath.value.trim()
  if (!path) return
  manualAdding.value = true
  try {
    const response = await request('/api/dash/superwatch/add', {
      method: 'POST',
      body: JSON.stringify({ name: path }),
    })
    const item = response?.item
    if (response?.error || item?.error) throw new Error(response?.error || item.error)
    const addedPath = typeof item?.name === 'string' ? item.name : path
    selected.value = withSet(selected.value, addedPath, true)
    await resolveSelectedDescriptor(addedPath)
    manualPath.value = ''
    manualAddOpen.value = false
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  } finally {
    manualAdding.value = false
  }
}

function applyRebindSummary(summary: { removed: string[] }): void {
  const next = new Set(selected.value)
  summary.removed.forEach(path => {
    next.delete(path)
    emit('selection-removed', path)
  })
  selected.value = next
  const descriptors = new Map(selectedDescriptors.value)
  summary.removed.forEach(path => descriptors.delete(path))
  selectedDescriptors.value = descriptors
}

async function resolveSelectedDescriptor(path: string): Promise<void> {
  const matches = await catalog.searchSymbols(path)
  const descriptor = matches.find(item => item.path === path)
  if (!descriptor) return
  const next = new Map(selectedDescriptors.value)
  next.set(path, descriptor)
  selectedDescriptors.value = next
}

async function refreshSelectedDescriptors(): Promise<void> {
  const paths = [...selected.value]
  const resolved = await Promise.all(paths.map(async path => {
    const matches = await catalog.searchSymbols(path).catch(() => [])
    return matches.find(item => item.path === path)
  }))
  selectedDescriptors.value = new Map(
    resolved.filter((item): item is SymbolDescriptor => Boolean(item)).map(item => [item.path, item]),
  )
}

function openCLayout(variable = ''): void {
  cLayoutVariable.value = variable
  cLayoutOpen.value = true
}

function closeCLayout(): void {
  if (catalog.applyingLayout.value) return
  cLayoutOpen.value = false
}

async function applyCLayout(): Promise<void> {
  try {
    const result = await catalog.applyCLayout(
      cLayoutVariable.value.trim(),
      cLayoutDefinition.value,
      cLayoutPack.value ? Number(cLayoutPack.value) : null,
    )
    applyRebindSummary(result.rebind)
    cLayoutOpen.value = false
    toast.success(tr(`已解析 ${result.layout.leaf_count} 个成员`, `Parsed ${result.layout.leaf_count} members`))
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  }
}

function toggleVisibility(path: string): void {
  emit('visibility-change', path, props.hiddenChannels?.has(path) ?? false)
}

function beginEdit(symbol: SymbolDescriptor): void {
  if (catalog.stale.value || !symbol.writable) return
  const current = props.latestValues[symbol.path]
  if (symbol.scalar_kind === 'bool') {
    editValues[symbol.path] = String(current ?? false)
  } else if (symbol.scalar_kind === 'enum') {
    const match = Object.entries(symbol.enum_values).find(([, value]) => value === current)
    editValues[symbol.path] = match?.[0] ?? Object.keys(symbol.enum_values)[0] ?? ''
  } else {
    editValues[symbol.path] = current === undefined ? '' : String(current)
  }
  editing.value = symbol.path
}

function typedValue(symbol: SymbolDescriptor): unknown {
  const raw = editValues[symbol.path]
  if (symbol.scalar_kind === 'bool') return raw === 'true'
  if (symbol.scalar_kind === 'enum') return raw
  if (symbol.scalar_kind === 'signed' || symbol.scalar_kind === 'unsigned') {
    const value = Number(raw)
    if (!Number.isInteger(value)) throw new Error(tr('请输入整数', 'Enter an integer'))
    return value
  }
  const value = Number(raw)
  if (!Number.isFinite(value)) throw new Error(tr('请输入有限数值', 'Enter a finite number'))
  return value
}

async function writeValue(symbol: SymbolDescriptor): Promise<void> {
  writing.value = withSet(writing.value, symbol.path, true)
  try {
    const result = await catalog.writeSymbol(symbol.path, typedValue(symbol))
    writeSuccess[symbol.path] = result.value
    editing.value = null
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  } finally {
    writing.value = withSet(writing.value, symbol.path, false)
  }
}

async function reparseSymbols(): Promise<void> {
  try {
    const summary = await catalog.reparse()
    applyRebindSummary(summary)
    await refreshSelectedDescriptors()
    toast.success(
      tr(`符号已更新：保留 ${summary.preserved.length}，更新 ${summary.updated.length}，移除 ${summary.removed.length}`, `Symbols updated: ${summary.preserved.length} preserved, ${summary.updated.length} updated, ${summary.removed.length} removed`),
    )
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  }
}

function formatValue(value: number | boolean | undefined): string {
  if (value === undefined) return '--'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toPrecision(7)
  return String(value)
}

onMounted(loadWorkspace)
watch(() => props.deviceConnected, connected => {
  if (connected && props.symbolLoaded) void loadWorkspace()
})
watch(() => props.symbolLoaded, loaded => {
  if (loaded && props.deviceConnected) void loadWorkspace()
})
watch(query, (next, previous) => {
  if (next.trim() && !previous.trim()) searchExpansionSnapshot = new Set(expanded.value)
  if (!next.trim() && previous.trim() && searchExpansionSnapshot) {
    expanded.value = searchExpansionSnapshot
    searchExpansionSnapshot = null
    searchItems.value = []
    searchRequest += 1
    return
  }
  const value = next.trim()
  if (!value) return
  const requestId = ++searchRequest
  void catalog.searchSymbols(value).then(items => {
    if (requestId === searchRequest) searchItems.value = items
  }).catch(cause => {
    if (requestId === searchRequest) toast.error(cause instanceof Error ? cause.message : String(cause))
  })
})
watch(tree, roots => {
  if (query.value.trim() || selectedOnly.value) return
  const valid = collectBranchKeys(roots)
  expanded.value = new Set([...expanded.value].filter(path => valid.has(path)))
  if (searchExpansionSnapshot) {
    searchExpansionSnapshot = new Set([...searchExpansionSnapshot].filter(path => valid.has(path)))
  }
})
</script>

<style scoped>
.symbol-panel {
  display: flex;
  flex-direction: column;
  min-width: 280px;
  min-height: 0;
  border-right: 1px solid var(--border);
  background: var(--surface);
}
.panel-toolbar { display: flex; gap: 6px; padding: 10px; border-bottom: 1px solid var(--border); }
.panel-toolbar .form-input { min-width: 0; flex: 1; }
.icon-button { display: grid; place-items: center; flex: 0 0 30px; width: 30px; height: 30px; padding: 0; border: 1px solid var(--border); background: transparent; color: var(--fg); cursor: pointer; }
.icon-button:disabled { color: var(--muted); cursor: default; }
.manual-add-row { display: flex; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.manual-add-row .form-input { min-width: 0; flex: 1; }
.panel-filters { display: flex; justify-content: space-between; padding: 7px 10px; color: var(--muted); font-size: 12px; border-bottom: 1px solid var(--border); }
.panel-filters label { display: flex; align-items: center; gap: 5px; }
.stale-banner { padding: 7px 10px; color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, transparent); font-size: 12px; }
.variable-groups { min-height: 0; overflow: auto; }
.variable-root-heading { margin: 0; padding: 7px 10px; color: var(--muted); background: var(--bg); font-size: 11px; font-weight: 600; }
.branch-row {
  align-items: center;
  display: flex;
  width: 100%;
  min-height: 32px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.branch-row:hover { background: color-mix(in srgb, var(--accent) 5%, var(--surface)); }
.branch-toggle {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  flex: 1;
  min-width: 0;
  min-height: 32px;
  gap: 5px;
  padding-top: 4px;
  padding-right: 8px;
  padding-bottom: 4px;
  border: 0;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  text-align: left;
}
.snapshot-button {
  display: grid;
  place-items: center;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  margin-right: 5px;
  padding: 0;
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.snapshot-button:hover, .snapshot-button.active { border-color: var(--accent); color: var(--accent); }
.snapshot-button.active { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.snapshot-button:disabled { color: var(--muted); cursor: default; opacity: .5; }
.array-snapshot-modal { width: min(420px, 100%); }
.snapshot-modal-help { margin: 0; color: var(--muted); font: 11px/1.5 var(--mono, ui-monospace, Consolas, monospace); overflow-wrap: anywhere; }
.snapshot-range-error { margin: 0; color: var(--danger); font-size: 11px; }
.branch-spinner { animation: branch-spin 0.8s linear infinite; }
@keyframes branch-spin { to { transform: rotate(360deg); } }
.container-row {
  display: grid;
  grid-template-columns: 18px minmax(70px, 1fr) minmax(64px, auto) auto;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-height: 36px;
  padding-top: 4px;
  padding-right: 10px;
  padding-bottom: 4px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  color: var(--fg);
  cursor: pointer;
  text-align: left;
}
.container-row:hover { background: color-mix(in srgb, var(--accent) 5%, var(--surface)); }
.container-type { overflow: hidden; color: var(--muted); font: 11px Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.container-state { color: var(--warn); font-size: 11px; white-space: nowrap; }
.branch-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 12px Consolas, monospace; }
.branch-count { color: var(--muted); font: 11px Consolas, monospace; }
.variable-row { border-bottom: 1px solid var(--border); }
.variable-row.selected { background: color-mix(in srgb, var(--accent) 7%, transparent); }
.variable-main { display: grid; grid-template-columns: 18px 24px minmax(100px, 1fr) 64px 66px 42px; align-items: center; gap: 5px; min-height: 36px; padding: 4px 8px; }
.visibility-slot { display: grid; place-items: center; width: 24px; height: 24px; }
.visibility-button { display: grid; place-items: center; width: 24px; height: 24px; padding: 0; border: 0; background: transparent; color: var(--accent); cursor: pointer; }
.visibility-button:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.visibility-button.hidden { color: var(--muted); }
.visibility-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.variable-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 0; background: transparent; color: var(--fg); cursor: pointer; text-align: left; font: 12px Consolas, monospace; }
.variable-type, .variable-value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font: 11px Consolas, monospace; }
.variable-value { color: var(--info); text-align: right; }
.edit-button { border: 0; background: transparent; color: var(--accent); cursor: pointer; font-size: 11px; }
.edit-button:disabled { color: var(--muted); cursor: default; }
.write-editor { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 6px; padding: 0 8px 8px 60px; }
.write-editor input, .write-editor select { min-width: 0; height: 28px; }
.write-editor .btn { min-height: 28px; padding: 3px 8px; }
.write-success { padding: 0 8px 7px 60px; color: var(--success); font-size: 11px; }
.empty-state { padding: 24px 12px; color: var(--muted); text-align: center; font-size: 12px; }
.modal-overlay { position: fixed; z-index: 1000; inset: 0; display: grid; place-items: center; padding: 20px; background: rgb(0 0 0 / 45%); }
.layout-modal { display: grid; gap: 12px; width: min(620px, 100%); max-height: calc(100vh - 40px); padding: 16px; border: 1px solid var(--border); background: var(--surface); box-shadow: 0 16px 50px rgb(0 0 0 / 25%); }
.layout-modal-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.layout-modal-header h3 { display: flex; align-items: center; gap: 7px; margin: 0; font-size: 15px; }
.layout-field { display: grid; grid-template-columns: 64px minmax(0, 1fr); align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }
.layout-definition { align-items: start; }
.layout-definition span { padding-top: 7px; }
.layout-definition textarea { min-height: 260px; resize: vertical; font: 12px/1.5 Consolas, monospace; white-space: pre; }
.layout-modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
@media (max-width: 560px) {
  .modal-overlay { padding: 10px; }
  .layout-modal { max-height: calc(100vh - 20px); padding: 12px; }
  .layout-field { grid-template-columns: 1fr; gap: 5px; }
  .layout-definition span { padding-top: 0; }
  .layout-definition textarea { min-height: 220px; }
}
</style>
