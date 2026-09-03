<template>
  <div class="global-status-bar">
    <div class="status-item">
      <span :class="['status-dot', connected ? 'dot-ok' : 'dot-err']"></span>
      <span class="status-text">{{ connected ? tr('已连接', 'Connected') : tr('未连接', 'Disconnected') }}</span>
    </div>
    <div class="status-divider"></div>
    <div class="status-item">
      <span v-if="configValid && !warningCount" class="status-icon icon-ok">✓</span>
      <span v-else-if="errorCount" class="status-icon icon-err">✕</span>
      <span v-else-if="warningCount" class="status-icon icon-warn">!</span>
      <span v-else class="status-icon icon-dim">—</span>
      <span class="status-text">
        <template v-if="!configStatus">{{ tr('未加载', 'Not loaded') }}</template>
        <template v-else-if="errorCount">{{ errorCount }} {{ tr('错误', 'errors') }}</template>
        <template v-else-if="warningCount">{{ warningCount }} {{ tr('警告', 'warnings') }}</template>
        <template v-else>{{ tr('配置正常', 'Configuration valid') }}</template>
      </span>
    </div>
    <div class="status-divider"></div>
    <div class="status-item">
      <span :class="['status-dot', microkeenAvailable ? 'dot-ok' : 'dot-dim']"></span>
      <span class="status-text">{{ microkeenAvailable ? tr('MICROKEEN 可用', 'MICROKEEN available') : tr('MICROKEEN 未找到', 'MICROKEEN not found') }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ConfigStatus, DeviceStatus, MicrokeenInfo } from '../../types/mklink'
import { tr } from '../../composables/useLanguage'

const props = defineProps<{
  deviceStatus: DeviceStatus
  configStatus: ConfigStatus | null
  microkeen: MicrokeenInfo | null
}>()

const connected = computed(() => props.deviceStatus.connected)
const configValid = computed(() => props.configStatus?.is_valid ?? false)
const errorCount = computed(() => props.configStatus?.errors?.length ?? 0)
const warningCount = computed(() => props.configStatus?.warnings?.length ?? 0)
const microkeenAvailable = computed(() => props.microkeen?.available ?? false)
</script>

<style scoped>
.global-status-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 16px;
  font-size: 13px;
}
.status-item {
  display: flex;
  align-items: center;
  gap: 6px;
}
.status-divider {
  width: 1px;
  height: 16px;
  background: var(--border);
}
.status-text {
  color: var(--muted);
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-ok { background: var(--success); }
.dot-err { background: #e74c3c; }
.dot-dim { background: var(--dim); }
.status-icon {
  font-size: 12px;
  font-weight: 700;
  width: 16px;
  height: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 3px;
}
.icon-ok { color: var(--success); }
.icon-err { color: #e74c3c; }
.icon-warn { color: var(--warn); }
.icon-dim { color: var(--dim); }
</style>
