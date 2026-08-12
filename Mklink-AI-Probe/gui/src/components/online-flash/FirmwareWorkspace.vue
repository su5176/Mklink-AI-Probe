<script setup lang="ts">
import { computed, ref } from 'vue'
import { isTauri } from '@tauri-apps/api/core'
import type { FormattedHexRow } from '../../lib/hexPreview'
import type { ImageInspection } from '../../types/onlineFlash'
import { tr } from '../../composables/useLanguage'
import { supportsTrackedFirmwarePicker } from '../../lib/filePicker'

const props = defineProps<{ file: File | null; sourcePath?: string; nativeDropActive?: boolean; baseAddress: string; baseError: string; inspection: ImageInspection | null; rows: FormattedHexRow[]; paddingTop: number; paddingBottom: number; loading: boolean; error: string }>()
const emit = defineEmits<{ file: [file: File | null]; browse: []; dropFiles: [files: File[]]; base: [value: string]; scroll: [top: number, height: number] }>()
const sourceName = computed(() => props.file?.name || props.sourcePath?.split(/[\\/]/).pop() || '')
const isBin = computed(() => sourceName.value.toLowerCase().endsWith('.bin'))
const fileInput = ref<HTMLInputElement | null>(null)
const dragging = ref(false)
const nativeApp = isTauri()
const trackedBrowserPicker = supportsTrackedFirmwarePicker()
function openFile() { fileInput.value?.click() }
function fileChanged(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  input.value = ''
  emit('file', file)
}
function dropped(event: DragEvent) {
  dragging.value = false
  const files = Array.from(event.dataTransfer?.files || [])
  if (files.length) emit('dropFiles', files)
}
function scrolled(event: Event) { const el = event.currentTarget as HTMLElement; emit('scroll', el.scrollTop, el.clientHeight) }
function address(value: number) { return `0x${value.toString(16).toUpperCase().padStart(8, '0')}` }
</script>

<template>
  <div
    class="firmware-workspace"
    :class="{ dragging: dragging || nativeDropActive }"
    data-testid="firmware-drop-zone"
    @dragenter.prevent="dragging = true"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="dropped"
  >
  <div class="firmware-toolbar">
    <button v-if="nativeApp || trackedBrowserPicker" data-testid="firmware-trigger" class="file-button" type="button" @click="emit('browse')">{{ tr('选择 BIN / HEX', 'Select BIN / HEX') }}</button>
    <label v-else data-testid="firmware-trigger" class="file-button" role="button" tabindex="0" @keydown.enter.prevent="openFile" @keydown.space.prevent="openFile">{{ tr('选择 BIN / HEX', 'Select BIN / HEX') }}<input ref="fileInput" class="visually-hidden" data-testid="firmware-input" type="file" accept=".bin,.hex" @change="fileChanged"></label>
    <span class="filename">{{ sourceName || tr('拖拽 BIN / HEX 到此处，或选择固件', 'Drop BIN / HEX here, or select firmware') }}</span>
    <label v-if="isBin" class="base-field">{{ tr('基地址', 'Base Address') }}<input data-testid="bin-base" :value="baseAddress" :placeholder="tr('如 0x08000000', 'e.g. 0x08000000')" @input="emit('base', ($event.target as HTMLInputElement).value)"></label>
    <span v-if="loading" class="inspection-status">{{ tr('自动检查中…', 'Inspecting…') }}</span>
    <span v-else-if="inspection" class="inspection-status inspection-ok">{{ tr('已自动检查', 'Inspected') }}</span>
  </div>
  <p v-if="baseError" data-testid="base-error" class="error">{{ baseError }}</p><p v-if="error" class="error">{{ error }}</p>
  <div v-if="inspection" class="metadata"><span>{{ inspection.format.toUpperCase() }}</span><span>{{ inspection.size }} bytes</span><span>{{ address(inspection.start) }} — {{ address(inspection.end) }}</span><span>SHA-256 {{ inspection.sha256.slice(0, 12) }}…</span></div>
  <div class="hex-head"><span>{{ tr('地址', 'Address') }}</span><span>00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F</span><span>ASCII</span></div>
  <div class="hex-scroll" @scroll="scrolled">
    <div :style="{ height: `${paddingTop}px` }" />
    <div v-for="row in rows" :key="row.address" class="hex-row"><span>{{ row.address }}</span><span class="cells"><i v-for="(cell, index) in row.hex" :key="index" :class="{ gap: cell === '--' }">{{ cell }}</i></span><span>{{ row.ascii }}</span></div>
    <div :style="{ height: `${paddingBottom}px` }" />
    <div v-if="!inspection" class="empty">{{ tr('选择已安装器件与固件后将自动检查，预览按需加载，不会一次渲染整个文件。', 'Select an installed target and firmware to inspect it. Preview data loads on demand.') }}</div>
  </div>
  </div>
</template>

<style scoped>
.firmware-workspace{position:relative;min-height:0;height:100%;display:flex;flex-direction:column;outline:2px solid transparent;outline-offset:-2px}.firmware-workspace.dragging{outline-color:var(--of-accent);background:color-mix(in srgb,var(--of-accent) 8%,transparent)}.firmware-toolbar{display:flex;align-items:center;gap:8px;padding:10px;border-bottom:1px solid var(--of-border)}.file-button{padding:7px 10px;border:1px solid var(--control-border);border-radius:5px;background:var(--of-input);color:var(--of-text);font-size:11px;cursor:pointer}.visually-hidden{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.file-button:focus-visible{outline:2px solid var(--focus-ring);outline-offset:2px}.filename{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--of-muted);font-size:11px}.inspection-status{margin-left:auto;color:var(--of-warn);font-size:10px;white-space:nowrap}.inspection-ok{color:var(--of-ok)}.base-field{margin-left:auto;display:flex;align-items:center;gap:5px;color:var(--of-muted);font-size:10px}.base-field+.inspection-status{margin-left:0}.base-field input{width:92px;padding:6px;border:1px solid var(--control-border);border-radius:4px;background:var(--of-input);color:var(--of-text);font-family:var(--of-mono)}.error{margin:5px 10px;color:var(--of-danger);font-size:11px}.metadata{display:flex;gap:14px;padding:7px 10px;border-bottom:1px solid var(--of-border);color:var(--of-muted);font-size:10px}.hex-head,.hex-row{display:grid;grid-template-columns:78px minmax(430px,1fr) 136px;align-items:center;white-space:pre}.hex-head{padding:6px 10px;background:color-mix(in srgb,var(--terminal-bg) 88%,var(--surface));color:#a8bbb8;font:10px var(--of-mono)}.hex-scroll{min-height:0;height:auto;flex:1;overflow:auto;text-align:left;background:var(--terminal-bg);font:11px/20px var(--of-mono)}.hex-row{height:20px;padding:0 10px;color:var(--terminal-text)}.cells{display:grid;grid-template-columns:repeat(16,2ch);column-gap:1ch}.cells i{font-style:normal;color:var(--terminal-text)}.cells i.gap{color:#718382}.empty{padding:50px 20px;text-align:center;color:var(--of-muted)}
</style>
