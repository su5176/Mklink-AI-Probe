<template>
  <div class="log-display-controls">
    <div class="log-format-switch" role="group" :aria-label="tr('日志显示格式', 'Log display format')">
      <button
        :data-testid="`${idPrefix}-log-text`" type="button"
        :class="{ active: mode === 'text' }" :aria-pressed="mode === 'text'"
        @click="$emit('update:mode', 'text')"
      >{{ tr('字符串', 'Text') }}</button>
      <button
        :data-testid="`${idPrefix}-log-hex`" type="button"
        :class="{ active: mode === 'hex' }" :aria-pressed="mode === 'hex'"
        @click="$emit('update:mode', 'hex')"
      >HEX</button>
    </div>
    <label class="timestamp-toggle">
      <input
        :data-testid="`${idPrefix}-log-timestamp`" type="checkbox"
        :checked="showTimestamp"
        @change="$emit('update:showTimestamp', ($event.target as HTMLInputElement).checked)"
      >
      <span>{{ tr('时间戳', 'Timestamp') }}</span>
    </label>
  </div>
</template>

<script setup lang="ts">
import { tr } from '../../composables/useLanguage'

export type LogDisplayMode = 'text' | 'hex'

defineProps<{
  idPrefix: string
  mode: LogDisplayMode
  showTimestamp: boolean
}>()

defineEmits<{
  'update:mode': [mode: LogDisplayMode]
  'update:showTimestamp': [show: boolean]
}>()
</script>

<style scoped>
.log-display-controls { display: inline-flex; align-items: center; gap: 9px; }
.log-format-switch { display: inline-flex; overflow: hidden; border: 1px solid var(--border); border-radius: 4px; }
.log-format-switch button { height: 26px; padding: 0 8px; border: 0; background: var(--surface); color: var(--muted); cursor: pointer; font-size: 11px; }
.log-format-switch button + button { border-left: 1px solid var(--border); }
.log-format-switch button.active { background: var(--accent); color: #fff; }
.timestamp-toggle { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); cursor: pointer; font-size: 11px; white-space: nowrap; }
.timestamp-toggle input { margin: 0; accent-color: var(--accent); }
</style>
