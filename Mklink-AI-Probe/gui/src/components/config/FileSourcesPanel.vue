<script setup lang="ts">
import { computed } from 'vue'
import { FolderOpen, ScanSearch } from '@lucide/vue'
import { isSameFileSourcePath, isSymbolFilePath } from '../../lib/desktopSettings'
import type { AxlStatus } from '../../types/mklink'
import { tr } from '../../composables/useLanguage'

const props = defineProps<{
  symbolPath: string
  symbolDisplayPath?: string
  connected: boolean
  symbolStatus: AxlStatus
  browsing?: boolean
  parsing?: boolean
}>()

const emit = defineEmits<{
  (event: 'update:symbolPath', value: string): void
  (event: 'browse-symbol'): void
  (event: 'parse'): void
}>()

function inputValue(event: Event): string {
  return (event.target as HTMLInputElement).value
}

const sourceMatches = computed(() => isSameFileSourcePath(
  props.symbolPath,
  props.symbolStatus.axf_path,
))
const sourcePending = computed(() => (
  props.symbolStatus.loaded
  && Boolean(props.symbolPath.trim())
  && !sourceMatches.value
))
const displayedSymbolPath = computed(() => props.symbolDisplayPath?.trim() || props.symbolPath)
const browserSymbolUpload = computed(() => Boolean(
  props.symbolDisplayPath?.trim()
  && !isSameFileSourcePath(props.symbolDisplayPath, props.symbolPath),
))
const activeSymbolPath = computed(() => (
  sourceMatches.value && browserSymbolUpload.value
    ? displayedSymbolPath.value
    : props.symbolStatus.axf_path
))
const parserBackend = computed(() => {
  if (props.symbolStatus.elf_backend === 'external') return tr('外部 GNU 工具', 'External GNU tools')
  const version = props.symbolStatus.builtin_elf_version
  return `${tr('内置', 'Built-in')} pyelftools${version ? ` ${version}` : ''}`
})
</script>

<template>
  <section class="card source-panel" aria-labelledby="file-sources-title">
    <header class="panel-header">
      <div>
        <h2 id="file-sources-title">{{ tr('文件来源', 'File Sources') }}</h2>
        <span
          data-testid="symbol-source-state"
          :class="['badge', symbolStatus.loaded && !sourcePending ? 'badge-ok' : 'badge-warn']"
        >
          {{ sourcePending ? tr('待解析', 'Pending') : symbolStatus.loaded ? tr('符号已加载', 'Symbols loaded') : tr('符号未加载', 'Symbols not loaded') }}
        </span>
      </div>
      <span v-if="symbolStatus.loaded" class="symbol-counts">
        {{ symbolStatus.variable_count || 0 }} {{ tr('个固定可读变量', 'fixed readable variables') }} · {{ symbolStatus.struct_count || 0 }} {{ tr('种结构体类型', 'struct types') }} · {{ symbolStatus.enum_count || 0 }} {{ tr('种枚举类型', 'enum types') }}
      </span>
    </header>

    <div
      v-if="symbolStatus.loaded && symbolStatus.axf_path"
      class="active-symbol-path"
      data-testid="active-symbol-path"
    >
      <span>{{ tr('当前加载', 'Loaded File') }}</span>
      <code :title="symbolStatus.axf_path || undefined">{{ activeSymbolPath }}</code>
      <span>{{ tr('解析后端', 'Parser') }}</span>
      <code data-testid="symbol-parser-backend">{{ parserBackend }}</code>
    </div>

    <div class="source-row">
      <label for="symbol-path">AXF / ELF</label>
      <div class="path-control">
        <input
          id="symbol-path"
          class="form-input path-input"
          data-testid="symbol-path"
          :value="displayedSymbolPath"
          :placeholder="tr('.axf 或 .elf 文件路径', '.axf or .elf file path')"
          :disabled="browsing || parsing"
          @input="emit('update:symbolPath', inputValue($event))"
        />
        <button
          class="btn icon-command"
          type="button"
          :title="tr('浏览 AXF 或 ELF 文件', 'Browse for AXF or ELF file')"
          data-testid="browse-symbol"
          :disabled="browsing || parsing"
          @click="emit('browse-symbol')"
        >
          <FolderOpen :size="15" aria-hidden="true" />
          {{ tr('浏览', 'Browse') }}
        </button>
      </div>
    </div>
    <div
      data-testid="symbol-path-validation"
      :class="['path-validation', { invalid: symbolPath.trim() && !isSymbolFilePath(symbolPath) }]"
    >
      {{ !symbolPath.trim() ? tr('未配置 AXF / ELF 文件', 'No AXF / ELF file configured') : browserSymbolUpload ? tr(`浏览器上传 · ${displayedSymbolPath}（解析文件已缓存到本机服务）`, `Browser upload · ${displayedSymbolPath} (cached by local service)`) : isSymbolFilePath(symbolPath) ? tr('路径格式有效', 'Valid path') : tr('仅支持 .axf、.elf 或 .out 文件', 'Only .axf, .elf, or .out files are supported') }}
    </div>

    <div v-if="symbolStatus.error" class="alert alert-error">{{ symbolStatus.error }}</div>

    <footer class="panel-actions">
      <span class="action-state" data-testid="files-auto-save">{{ tr('路径修改后自动保存', 'Path is saved automatically') }}</span>
      <button
        class="btn btn-primary"
        type="button"
        data-testid="parse-symbols"
        :disabled="parsing || !connected || !isSymbolFilePath(symbolPath)"
        @click="emit('parse')"
      >
        <ScanSearch :size="15" aria-hidden="true" />
        {{ parsing ? tr('解析中...', 'Parsing...') : tr('解析符号', 'Parse Symbols') }}
      </button>
      <span v-if="!connected" class="action-state">{{ tr('需先连接设备', 'Connect a device first') }}</span>
    </footer>
  </section>
</template>

<style scoped>
.source-panel {
  min-height: 270px;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 22px;
}

.panel-header > div {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-header h2 {
  font-size: 15px;
  font-weight: 600;
}

.symbol-counts,
.action-state {
  color: var(--dim);
  font-size: 12px;
}

.active-symbol-path {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr);
  gap: 12px;
  margin: -10px 0 18px;
  color: var(--dim);
  font-size: 11px;
}

.active-symbol-path span {
  text-align: right;
}

.active-symbol-path code {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--muted);
  font-family: var(--font-mono);
}

.source-row {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr);
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}

.path-validation {
  margin: -8px 0 12px 104px;
  color: var(--dim);
  font-size: 11px;
}

.path-validation.invalid {
  color: var(--danger, #dc2626);
}

.source-row label {
  color: var(--muted);
  font-size: 13px;
  text-align: right;
}

.path-control {
  display: flex;
  gap: 8px;
  min-width: 0;
}

.path-input {
  min-width: 0;
  font-family: var(--font-mono);
  font-size: 12px;
}

.icon-command,
.panel-actions .btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
}

.panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 20px;
  padding-left: 104px;
}

@media (max-width: 720px) {
  .panel-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .source-row {
    grid-template-columns: 1fr;
    gap: 6px;
  }

  .source-row label {
    text-align: left;
  }

  .panel-actions {
    padding-left: 0;
    flex-wrap: wrap;
  }

  .path-validation {
    margin-left: 0;
  }
}
</style>
