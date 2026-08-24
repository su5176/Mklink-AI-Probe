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
    <div class="section-nav-heading">
      <span>DEVICE SETUP</span>
      <strong>{{ tr('设备与连接', 'Device & Connection') }}</strong>
    </div>
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
  width: 184px;
  flex: 0 0 184px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  background: var(--surface);
  box-shadow: var(--shadow-card);
}

.section-nav-heading {
  display: grid;
  gap: 3px;
  padding: 3px 8px 12px;
  margin-bottom: 3px;
  border-bottom: 1px solid var(--line);
}

.section-nav-heading span {
  color: var(--brand-text);
  font: 700 9px/1 var(--font-mono);
  letter-spacing: .13em;
}

.section-nav-heading strong {
  color: var(--text);
  font-size: 13px;
  font-weight: 700;
}

.section-button {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 40px;
  padding: 0 12px;
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard);
}

.section-button:hover {
  color: var(--fg);
  background: var(--surface);
  border-color: var(--border-subtle);
}

.section-button.active {
  color: var(--nav-active-text);
  background: var(--nav-active-bg);
  border-color: var(--line-strong);
  box-shadow: inset 3px 0 0 var(--brand-secondary);
  font-weight: 650;
}

@media (max-width: 760px) {
  .section-nav {
    width: 100%;
    flex-basis: auto;
    flex-direction: row;
    overflow-x: auto;
    padding: 8px;
    scrollbar-width: none;
  }

  .section-nav::-webkit-scrollbar { display: none; }

  .section-nav-heading { display: none; }

  .section-button {
    width: auto;
    min-width: max-content;
  }
}
</style>
