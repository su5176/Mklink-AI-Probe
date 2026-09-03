<script setup lang="ts">
import { computed } from 'vue'
import type { ProbeFirmwareCheck, FirmwareInfo } from '../../types/mklink'
import { useTauri } from '../../composables/useTauri'
import { tr } from '../../composables/useLanguage'

const props = defineProps<{ check: ProbeFirmwareCheck }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'recheck'): void
}>()

const { openInExplorer } = useTauri()

const steps = computed(() =>
  props.check.instructions
    .split('\n')
    .filter((l: string) => l.trim().length > 0)
)

async function onOpenDir() {
  if (props.check.firmware_dir) {
    await openInExplorer(props.check.firmware_dir)
  }
}

function fwLabel(fw: FirmwareInfo): string {
  return `${fw.name} (${fw.model}, ${fw.version})`
}
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')">
    <div class="modal firmware-modal" role="dialog" aria-labelledby="fw-title">
      <header class="modal-header">
        <h2 id="fw-title">{{ tr('探针固件需要升级', 'Probe Firmware Update Required') }}</h2>
        <button class="close-btn" :aria-label="tr('关闭', 'Close')" @click="emit('close')">×</button>
      </header>
      <div class="modal-body">
        <section class="fw-steps">
          <h3>{{ tr('升级步骤', 'Update Steps') }}</h3>
          <ol>
            <li v-for="(line, i) in steps" :key="i">{{ line }}</li>
          </ol>
        </section>
        <section class="fw-files">
          <h3>{{ tr('固件', 'Firmware') }}</h3>
          <div v-if="check.recommended_uf2" class="fw-card recommended">
            <strong>{{ tr('推荐：', 'Recommended:') }}</strong>
            <code>{{ fwLabel(check.recommended_uf2) }}</code>
            <div class="fw-path">{{ check.recommended_uf2.path }}</div>
          </div>
          <div v-else>
            <p>{{ tr('无法识别探针型号，请从下方任选一个 UF2：', 'Probe model could not be identified. Choose a UF2 below:') }}</p>
            <div v-for="fw in check.all_uf2s" :key="fw.name" class="fw-card">
              <code>{{ fwLabel(fw) }}</code>
              <div class="fw-path">{{ fw.path }}</div>
            </div>
          </div>
          <button v-if="check.firmware_dir" class="open-dir-btn" @click="onOpenDir">
            {{ tr('打开 MK-Firmware 所在位置', 'Open MK-Firmware Location') }}
          </button>
        </section>
      </div>
      <footer class="modal-footer">
        <button class="recheck-btn" @click="emit('recheck')">{{ tr('重新检测', 'Check Again') }}</button>
        <button class="close-action" @click="emit('close')">{{ tr('关闭', 'Close') }}</button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.firmware-modal {
  background: var(--color-bg-elevated, #fff);
  border-radius: 8px;
  width: min(800px, 90vw);
  max-height: 85vh;
  display: flex;
  flex-direction: column;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  border-bottom: 1px solid var(--color-border, #ddd);
}

.modal-body {
  padding: 16px 24px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  overflow: auto;
}

.modal-footer {
  padding: 12px 24px;
  border-top: 1px solid var(--color-border, #ddd);
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.fw-card {
  background: var(--color-bg, #f7f7f7);
  border: 1px solid var(--color-border, #ddd);
  border-radius: 4px;
  padding: 8px 12px;
  margin: 6px 0;
}

.fw-card.recommended {
  border-color: var(--color-primary, #3b82f6);
}

.fw-path {
  font-family: monospace;
  font-size: 0.85em;
  color: var(--color-text-muted, #666);
  margin-top: 4px;
  word-break: break-all;
}

.fw-steps ol {
  padding-left: 1.2em;
}

.fw-steps li {
  margin: 4px 0;
}

.open-dir-btn {
  margin-top: 12px;
  padding: 6px 12px;
}

.close-btn {
  background: none;
  border: 0;
  font-size: 1.5em;
  cursor: pointer;
}
</style>
