<template>
  <div class="symbols-tab">
    <SetupHint
      v-if="!deviceConnected"
      kind="device"
      :message="tr('符号表的运行时读取需要连接 MKLink 设备。', 'Runtime symbol inspection requires an MKLink device connection.')"
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
      :message="hasSymbolSource ? tr('已选择 AXF / ELF，解析后即可查看符号。', 'An AXF / ELF file is selected. Parse it to inspect symbols.') : tr('加载 AXF / ELF 以查看变量、类型和地址。', 'Load AXF / ELF to inspect variables, types, and addresses.')"
      :primary-label="hasSymbolSource ? tr('解析已选文件', 'Parse Selected File') : tr('加载 AXF / ELF', 'Load AXF / ELF')"
      :busy="loadingSymbols"
      @primary="hasSymbolSource ? parseSelectedSymbols() : loadSymbolFile()"
    />
    <template v-else>
      <div class="sym-controls">
        <input
          v-model="query"
          data-testid="symbol-search"
          class="form-input"
          :placeholder="tr('搜索变量名或类型', 'Search variable name or type')"
        />
        <button
          v-if="catalog.stale.value"
          class="btn btn-secondary"
          type="button"
          :disabled="catalog.reparsing.value"
          @click="reparseSymbols"
        >
          {{ catalog.reparsing.value ? tr('解析中', 'Parsing') : tr('重新解析', 'Reparse') }}
        </button>
      </div>

      <div class="sym-summary">
        <span>{{ tr(`第 ${catalog.generation.value} 代`, `Generation ${catalog.generation.value}`) }}</span>
        <span>{{ rows.length }} {{ tr('个节点', 'nodes') }}</span>
        <span v-if="catalog.stale.value" class="stale-label">{{ tr('AXF 已变化', 'AXF changed') }}</span>
      </div>
      <div v-if="catalog.loading.value" class="sym-empty">{{ tr('正在加载符号表...', 'Loading symbols...') }}</div>
      <div v-else-if="rows.length" class="sym-results">
        <template v-for="row in rows" :key="row.node.key">
          <button
            v-if="row.node.kind === 'branch' || row.node.kind === 'range'"
            class="sym-item sym-branch"
            type="button"
            :data-symbol="row.node.key"
            :style="{ paddingLeft: rowIndent(row.depth) }"
            @click="toggleBranch(row.node)"
          >
            <LoaderCircle v-if="catalog.browseLoading.value.has(row.node.key)" class="sym-spinner" :size="14" aria-hidden="true" />
            <ChevronDown v-else-if="row.expanded" :size="14" aria-hidden="true" />
            <ChevronRight v-else :size="14" aria-hidden="true" />
            <span class="sym-name">{{ row.node.label }}</span>
            <span class="sym-type">{{ row.node.browse?.type_name }}</span>
            <span class="sym-size">{{ row.node.childCount ?? '' }}</span>
          </button>
          <button
            v-else-if="row.node.descriptor"
            class="sym-item"
            type="button"
            :data-symbol="row.node.descriptor.path"
            :style="{ paddingLeft: rowIndent(row.depth) }"
            @click="selectSymbol(row.node.descriptor.path)"
          >
            <span class="sym-tree-spacer" aria-hidden="true"></span>
            <span class="sym-name">{{ row.node.label }}</span>
            <span class="sym-type">{{ row.node.descriptor.type_name }}</span>
            <span class="sym-addr">{{ formatAddr(row.node.descriptor.address) }}</span>
            <span class="sym-size">{{ row.node.descriptor.size }}B</span>
          </button>
          <div
            v-else-if="row.node.container"
            class="sym-item sym-container"
            :style="{ paddingLeft: rowIndent(row.depth) }"
          >
            <span class="sym-tree-spacer" aria-hidden="true"></span>
            <span class="sym-name">{{ row.node.label }}</span>
            <span class="sym-type">{{ row.node.container.type_name }}</span>
            <span class="sym-addr">{{ formatAddr(row.node.container.address) }}</span>
            <span class="sym-size">{{ row.node.container.size }}B</span>
          </div>
        </template>
      </div>
      <div v-else class="sym-empty">
        {{ query ? tr('无匹配变量', 'No matching variables') : tr('当前 AXF 中没有可运行时读取的变量', 'The current AXF has no runtime-readable variables') }}
      </div>

      <div v-if="selectedType" class="sym-detail">
        <h4>{{ tr('类型信息:', 'Type Information:') }} {{ selectedType.name }}</h4>
        <table v-if="selectedType.found" class="desc-table">
          <tbody>
            <tr><th>{{ tr('类型', 'Type') }}</th><td>{{ selectedType.type }}</td></tr>
            <tr><th>{{ tr('大小', 'Size') }}</th><td>{{ selectedType.size }} bytes</td></tr>
            <tr><th>{{ tr('地址', 'Address') }}</th><td>{{ formatAddr(selectedType.address) }}</td></tr>
          </tbody>
        </table>
        <div v-if="selectedType.members?.length" class="sym-members">
          <h5>{{ tr('成员', 'Members') }}</h5>
          <div v-for="(member, index) in selectedType.members" :key="index" class="sym-member">
            {{ JSON.stringify(member) }}
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { ChevronDown, ChevronRight, LoaderCircle } from '@lucide/vue'
import { useSymbolsApi } from '../../composables/useDashboard'
import { useSymbolCatalog } from '../../composables/useSymbolCatalog'
import { useToast } from '../../composables/useToast'
import { useDashboardSetup } from '../../composables/useDashboardSetup'
import { buildBrowseTree, buildSymbolTree, visibleSymbolRows } from '../../lib/symbolTree'
import type { SymbolTreeNode } from '../../lib/symbolTree'
import type { SymbolDescriptor, SymbolTypeInfo } from '../../types/mklink'
import { tr } from '../../composables/useLanguage'
import SetupHint from './SetupHint.vue'

const props = withDefaults(defineProps<{
  deviceConnected: boolean
  symbolLoaded?: boolean
  symbolError?: string
}>(), {
  symbolLoaded: true,
  symbolError: '',
})

const catalog = useSymbolCatalog()
const symbols = useSymbolsApi()
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
const selectedType = ref<SymbolTypeInfo | null>(null)
const expanded = shallowRef(new Set<string>())
const searchItems = shallowRef<SymbolDescriptor[]>([])
let searchRequest = 0

const tree = computed(() => query.value.trim()
  ? buildSymbolTree(searchItems.value, catalog.containers.value)
  : buildBrowseTree(catalog.browseRoots.value, catalog.browseChildren.value))
const rows = computed(() => visibleSymbolRows(tree.value, {
  expanded: expanded.value,
  selected: new Set<string>(),
  query: query.value,
  selectedOnly: false,
}))

async function loadCatalog(): Promise<void> {
  if (!props.deviceConnected || !props.symbolLoaded) return
  try {
    await catalog.ensureLoaded()
    await catalog.refreshStatus().catch(() => undefined)
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause)
    if (!message.includes('No DWARF')) toast.error(message)
  }
}

async function reparseSymbols(): Promise<void> {
  try {
    const summary = await catalog.reparse()
    toast.success(
      tr(`符号已更新：保留 ${summary.preserved.length}，更新 ${summary.updated.length}，移除 ${summary.removed.length}`, `Symbols updated: ${summary.preserved.length} preserved, ${summary.updated.length} updated, ${summary.removed.length} removed`),
    )
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  }
}

async function toggleBranch(node: SymbolTreeNode): Promise<void> {
  if (query.value.trim()) return
  if (expanded.value.has(node.key)) {
    expanded.value = new Set([...expanded.value].filter(key => key !== node.key))
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
  expanded.value = new Set(expanded.value).add(node.key)
}

function rowIndent(depth: number): string {
  return `${9 + depth * 16}px`
}

async function selectSymbol(path: string): Promise<void> {
  try {
    selectedType.value = await symbols.typeinfo(path)
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : String(cause))
  }
}

function formatAddr(address: unknown): string {
  if (address == null) return '-'
  if (typeof address === 'number') {
    return `0x${address.toString(16).toUpperCase().padStart(8, '0')}`
  }
  return String(address)
}

let statusTimer: ReturnType<typeof setInterval> | undefined
let statusBusy = false
let disposed = false
async function refreshSourceStatus(): Promise<void> {
  if (disposed || statusBusy || !props.deviceConnected || !props.symbolLoaded) return
  statusBusy = true
  try {
    const generation = catalog.generation.value
    await catalog.refreshStatus()
    if (disposed || generation === catalog.generation.value) return
    selectedType.value = null
    expanded.value = new Set()
    const requestId = ++searchRequest
    const key = query.value.trim()
    searchItems.value = []
    if (key) {
      const items = await catalog.searchSymbols(key)
      if (!disposed && requestId === searchRequest) searchItems.value = items
    }
  } catch {
    // Connection/parse failures are surfaced by the dashboard and catalog.
  } finally {
    statusBusy = false
  }
}
onMounted(() => {
  void loadCatalog()
  statusTimer = setInterval(() => { void refreshSourceStatus() }, 2000)
})
onUnmounted(() => {
  disposed = true
  searchRequest += 1
  clearInterval(statusTimer)
})
watch(() => props.deviceConnected, connected => {
  if (connected && props.symbolLoaded) void loadCatalog()
})
watch(() => props.symbolLoaded, loaded => {
  if (loaded && props.deviceConnected) void loadCatalog()
})
watch(query, value => {
  const key = value.trim()
  if (!key) {
    searchItems.value = []
    searchRequest += 1
    return
  }
  const requestId = ++searchRequest
  void catalog.searchSymbols(key).then(items => {
    if (requestId === searchRequest) searchItems.value = items
  }).catch(cause => {
    if (requestId === searchRequest) toast.error(cause instanceof Error ? cause.message : String(cause))
  })
})
</script>

<style scoped>
.symbols-tab { display: flex; flex-direction: column; gap: 10px; min-height: 0; }
.sym-controls { display: flex; gap: 8px; }
.sym-controls .form-input { flex: 1; min-width: 0; }
.sym-summary { display: flex; gap: 14px; color: var(--muted); font-size: 12px; }
.stale-label { color: var(--warn); }
.sym-results { max-height: 420px; overflow-y: auto; border: 1px solid var(--border); }
.sym-item {
  display: grid;
  grid-template-columns: 16px minmax(180px, 1fr) 120px 100px 44px;
  gap: 12px;
  width: 100%;
  padding: 7px 9px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
  font-family: Consolas, monospace;
  font-size: 12px;
}
.sym-branch { grid-template-columns: 16px minmax(180px, 1fr) 120px 44px; }
.sym-container { cursor: default; }
.sym-tree-spacer { width: 14px; }
.sym-spinner { animation: sym-spin 0.8s linear infinite; }
@keyframes sym-spin { to { transform: rotate(360deg); } }
.sym-item:last-child { border-bottom: 0; }
.sym-item:hover { background: var(--surface); }
.sym-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--fg); }
.sym-type, .sym-addr { color: var(--info); }
.sym-size { color: var(--muted); text-align: right; }
.sym-empty { color: var(--muted); padding: 16px; text-align: center; }
.sym-detail { border-top: 1px solid var(--border); padding-top: 12px; }
.sym-detail h4 { margin: 0 0 8px; font-size: 13px; }
.sym-members { margin-top: 8px; }
.sym-members h5 { margin: 0 0 4px; font-size: 12px; }
.sym-member { padding: 2px 0; color: var(--muted); font: 11px Consolas, monospace; }
.alert-warn { color: var(--warn); padding: 8px; border: 1px solid var(--warn); border-radius: 4px; }

@media (max-width: 720px) {
  .sym-item { grid-template-columns: 16px minmax(140px, 1fr) 90px 44px; }
  .sym-branch { grid-template-columns: 16px minmax(140px, 1fr) 90px 44px; }
  .sym-addr { display: none; }
}
</style>
