<script setup lang="ts">
import { Cable, FileCode, Radio, Server, Upload } from '@lucide/vue'
import { computed } from 'vue'
import { tr } from '../../composables/useLanguage'

export type ConfigSection = 'local' | 'files' | 'remote' | 'serve' | 'firmware'

defineProps<{ modelValue: ConfigSection }>()

const emit = defineEmits<{
  (event: 'update:modelValue', value: ConfigSection): void
}>()

const sections = computed(() => [
  { id: 'local' as const, label: tr('本地设备', 'Local Device'), icon: Cable },
  { id: 'files' as const, label: tr('文件来源', 'File Sources'), icon: FileCode },
  { id: 'remote' as const, label: tr('远程连接', 'Remote Connection'), icon: Radio },
  { id: 'serve' as const, label: tr('启动服务', 'Start Service'), icon: Server },
  { id: 'firmware' as const, label: tr('固件升级', 'Firmware Update'), icon: Upload },
])
</script>

<template>
  <nav class="section-nav" :aria-label="tr('配置区域', 'Configuration sections')">
    <button
      v-for="section in sections"
      :key="section.id"
      type="button"
      :class="['section-button', { active: modelValue === section.id }]"
      :aria-current="modelValue === section.id ? 'page' : undefined"
      :data-testid="`config-section-${section.id}`"
      @click="emit('update:modelValue', section.id)"
    >
      <component :is="section.icon" :size="17" :stroke-width="1.8" aria-hidden="true" />
      <span data-testid="config-section">{{ section.label }}</span>
    </button>
  </nav>
</template>

<style scoped>
.section-nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 176px;
  flex: 0 0 176px;
}

.section-button {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 38px;
  padding: 0 12px;
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.section-button:hover {
  color: var(--fg);
  background: var(--surface);
  border-color: var(--border-subtle);
}

.section-button.active {
  color: var(--accent);
  background: #f3ece6;
  border-color: var(--border);
  font-weight: 600;
}

@media (max-width: 760px) {
  .section-nav {
    width: 100%;
    flex-basis: auto;
    flex-direction: row;
    overflow-x: auto;
  }

  .section-button {
    width: auto;
    min-width: max-content;
  }
}
</style>
