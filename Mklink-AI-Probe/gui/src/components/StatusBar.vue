<template>
  <div class="status-bar">
    <!-- Backend health indicator -->
    <span class="status-item" v-if="backendState === 'starting'">
      <span class="status-dot dot-starting"></span>
      <span class="status-label">{{ tr('启动中...', 'Starting...') }}</span>
    </span>
    <span class="status-item" v-else-if="backendState === 'alive'">
      <span class="status-dot dot-ok"></span>
      <span class="status-label">
        {{ tr('后端正常', 'Backend online') }}<template v-if="backendPort"> · {{ backendPort }}</template>
      </span>
    </span>
    <span class="status-item" v-else>
      <span class="status-dot dot-err"></span>
      <span class="status-label">{{ tr('后端离线', 'Backend offline') }}</span>
      <button
        v-if="isTauri"
        class="btn btn-sm btn-danger"
        @click="handleRestart"
      >
        {{ tr('重启服务', 'Restart Service') }}
      </button>
    </span>
    <span class="status-divider"></span>
    <!-- Device connection status -->
    <span :class="['badge', deviceStatus.connected ? 'badge-ok' : 'badge-err']">
      {{ deviceStatus.connected ? tr('已连接', 'Connected') : tr('未连接', 'Disconnected') }}
    </span>
    <span v-if="deviceStatus.idcode" class="badge badge-info">{{ deviceStatus.idcode }}</span>
    <span v-if="wsConnected" class="badge badge-warn">WS</span>
  </div>
</template>

<script setup lang="ts">
import { useMklinkApi } from '../composables/useMklinkApi'
import { useMklinkWs } from '../composables/useMklinkWs'
import { useBackendHealth } from '../composables/useBackendHealth'
import { tr } from '../composables/useLanguage'

const { deviceStatus } = useMklinkApi()
const { wsConnected } = useMklinkWs()
const { backendState, backendPort, isTauri, restart } = useBackendHealth()

async function handleRestart() {
  try {
    await restart()
  } catch (e) {
    console.error('Backend restart failed:', e)
  }
}
</script>

<style scoped>
.status-bar {
  min-height: var(--control-height);
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 5px 3px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--surface-muted);
  box-shadow: var(--shadow-control);
}

.status-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--text-soft);
}

.status-label {
  white-space: nowrap;
}

.status-divider {
  width: 1px;
  height: 14px;
  background: var(--border);
  margin: 0 2px;
}

/* Pulsing animation for "starting" dot */
.dot-starting {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--warn, #b58a1b);
  animation: pulse-dot 1.2s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}

.btn-sm {
  padding: 0 8px;
  font-size: 11px;
  height: 22px;
  line-height: 22px;
}
</style>
