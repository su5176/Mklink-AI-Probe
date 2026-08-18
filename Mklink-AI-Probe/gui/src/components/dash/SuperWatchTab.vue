<template>
  <div class="superwatch-workspace" :style="{ gridTemplateColumns: `${panelWidth}px 5px minmax(0, 1fr)` }">
    <SymbolVariablePanel
      :device-connected="deviceConnected"
      :symbol-loaded="symbolLoaded"
      :symbol-error="symbolError"
      :latest-values="latestValues"
      :hidden-channels="hiddenChannels"
      :snapshot-path="snapshotPath"
      @visibility-change="setChannelVisibility"
      @selection-removed="clearChannelVisibility"
      @snapshot-change="snapshotPath = $event"
    />
    <div class="workspace-resizer" :title="tr('调整变量目录宽度', 'Resize variable catalog')" @mousedown="startResize"></div>
    <div class="waveform-pane" :class="{ 'has-array-snapshot': snapshotPath }">
      <WaveformViewer
        mode="SuperWatch"
        :device-connected="deviceConnected"
        :hidden-channels="hiddenChannels"
        @latest-values="latestValues = $event"
      />
      <ArraySnapshotViewer
        v-if="snapshotPath"
        :path="snapshotPath"
        :device-connected="deviceConnected"
        @close="snapshotPath = null"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import SymbolVariablePanel from './SymbolVariablePanel.vue'
import WaveformViewer from './WaveformViewer.vue'
import ArraySnapshotViewer from './ArraySnapshotViewer.vue'
import { tr } from '../../composables/useLanguage'
import { API_BASE } from '../../lib/runtimeEndpoint'

const props = withDefaults(defineProps<{
  deviceConnected: boolean
  symbolLoaded?: boolean
  symbolError?: string
}>(), {
  symbolLoaded: true,
  symbolError: '',
})

const panelWidth = ref(340)
const latestValues = shallowRef<Record<string, number | boolean>>({})
const hiddenChannels = shallowRef(new Set<string>())
const snapshotPath = ref<string | null>(null)
let resizeStartX = 0
let resizeStartWidth = 0

async function loadSnapshotSelection(): Promise<void> {
  if (!props.deviceConnected) {
    snapshotPath.value = null
    return
  }
  try {
    const response = await fetch(`${API_BASE}/api/dash/superwatch/array-snapshot`)
    if (!response.ok) return
    const payload = await response.json()
    snapshotPath.value = payload?.snapshot?.name ?? null
  } catch { /* retry when the device or workspace state changes */ }
}

function setChannelVisibility(path: string, visible: boolean): void {
  const next = new Set(hiddenChannels.value)
  if (visible) next.delete(path)
  else next.add(path)
  hiddenChannels.value = next
}

function clearChannelVisibility(path: string): void {
  if (!hiddenChannels.value.has(path)) return
  const next = new Set(hiddenChannels.value)
  next.delete(path)
  hiddenChannels.value = next
}

function startResize(event: MouseEvent): void {
  resizeStartX = event.clientX
  resizeStartWidth = panelWidth.value
  document.addEventListener('mousemove', resizePanel)
  document.addEventListener('mouseup', stopResize, { once: true })
}

function resizePanel(event: MouseEvent): void {
  panelWidth.value = Math.min(520, Math.max(280, resizeStartWidth + event.clientX - resizeStartX))
}

function stopResize(): void {
  document.removeEventListener('mousemove', resizePanel)
}

onUnmounted(() => {
  document.removeEventListener('mousemove', resizePanel)
  document.removeEventListener('mouseup', stopResize)
})

onMounted(loadSnapshotSelection)
watch(() => props.deviceConnected, loadSnapshotSelection)
</script>

<style scoped>
.superwatch-workspace {
  display: grid;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}
.workspace-resizer {
  width: 5px;
  background: var(--border);
  cursor: col-resize;
}
.workspace-resizer:hover { background: var(--accent); }
.waveform-pane {
  display: grid;
  grid-template-rows: minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.waveform-pane.has-array-snapshot { grid-template-rows: minmax(260px, 1fr) minmax(190px, 38%); }

@media (max-width: 760px) {
  .superwatch-workspace {
    grid-template-columns: 1fr !important;
    grid-template-rows: minmax(220px, 38vh) minmax(360px, 1fr);
    overflow: auto;
  }
  .workspace-resizer { display: none; }
}
</style>
