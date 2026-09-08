<script setup lang="ts">
import { computed, ref } from 'vue'
import { useConfirmation } from '../../composables/useConfirmation'
import { Play, Square } from '@lucide/vue'
import type { JobAction, JobState } from '../../types/onlineFlash'
import { tr } from '../../composables/useLanguage'
const props = defineProps<{ actions: JobAction[]; canStart: boolean; active: boolean; stopping: boolean; state: JobState | null; totalProgress: number; progressLabel?: string; progressState?: string; unlockEnabled?: boolean; lockEnabled?: boolean; securityReason?: string; unlockErasesEeprom?: boolean; unlockErasesBackupRegisters?: boolean }>()
const emit = defineEmits<{ actions: [actions: JobAction[]]; start: []; stop: [] }>()
const confirm = useConfirmation()
const confirming = ref(false)
const choices = computed<Array<{ value: JobAction; label: string }>>(() => [{value:'connect',label:tr('连接', 'Connect')},{value:'unlock',label:tr('解锁', 'Unlock')},{value:'erase',label:tr('擦除', 'Erase')},{value:'program',label:tr('烧录', 'Program')},{value:'verify',label:tr('校验', 'Verify')},{value:'lock',label:tr('加锁', 'Lock')},{value:'reset',label:tr('复位', 'Reset')},{value:'disconnect',label:tr('断开', 'Disconnect')}])
const mandatory = new Set<JobAction>(['connect', 'disconnect'])
function available(action: JobAction): boolean {
  if (action === 'unlock') return props.unlockEnabled === true
  if (action === 'lock') return props.lockEnabled === true
  return true
}
async function confirmSecurityAction(action: JobAction): Promise<boolean> {
  const extraData = props.unlockErasesEeprom || props.unlockErasesBackupRegisters
  if (action === 'unlock') return confirm(tr(
    extraData
      ? '解锁会关闭读保护并强制整片擦除，Flash、数据 EEPROM 和备份寄存器中的全部数据都会永久删除。确定勾选“解锁”？'
      : '解锁会关闭读保护并强制整片擦除，Flash 中的全部数据都会永久删除。确定勾选“解锁”？',
    extraData
      ? 'Unlocking disables read protection and forces a full-chip erase. All Flash, data EEPROM, and backup-register data will be permanently deleted. Select Unlock?'
      : 'Unlocking disables read protection and forces a full-chip erase. All Flash data will be permanently deleted. Select Unlock?',
  ))
  if (action === 'lock') return confirm(tr(
    '加锁会在校验完成后启用可逆读保护，并在复位后限制 Flash 读取和调试访问。以后解锁仍会整片擦除。确定勾选“加锁”？',
    'Locking enables reversible read protection after verification and restricts Flash reads and debug access after reset. A later unlock will still erase the entire chip. Select Lock?',
  ))
  return true
}
async function toggle(action: JobAction, input: HTMLInputElement) {
  const checked = input.checked
  if (mandatory.has(action) || !available(action) || props.active || confirming.value) return
  if (checked && (action === 'unlock' || action === 'lock')) {
    input.checked = false
    confirming.value = true
    const accepted = await confirmSecurityAction(action)
    confirming.value = false
    if (!accepted || props.active || !available(action)) return
  }
  const selected = new Set(props.actions)
  if (checked) {
    selected.add(action)
    if (action === 'lock') { selected.add('verify'); selected.add('reset') }
  } else {
    selected.delete(action)
    if (action === 'verify') selected.delete('lock')
    if (action === 'reset') selected.delete('lock')
  }
  emit('actions', choices.value.map(choice => choice.value).filter(value => mandatory.has(value) || selected.has(value)))
}
const stateLabel = (state: JobState | null) => state === 'stopping' ? 'STOPPING' : state === 'stopped' ? tr('已停止', 'STOPPED') : state?.toUpperCase() || tr('待命', 'IDLE')
const totalPercent = computed(() => Math.round(Math.min(1, Math.max(0, props.totalProgress)) * 100))
</script>
<template>
  <div class="action-bar">
    <div class="action-choices">
      <label v-for="choice in choices" :key="choice.value" :class="{ unavailable: !available(choice.value) }" :title="!available(choice.value) ? securityReason : undefined">
        <input :data-testid="`action-${choice.value}`" type="checkbox" :checked="actions.includes(choice.value)" :disabled="active || confirming || mandatory.has(choice.value) || !available(choice.value)" @change="toggle(choice.value, $event.target as HTMLInputElement)">
        {{ choice.label }}
      </label>
    </div>
    <div class="progress-block">
      <div class="progress-meta">
        <span class="progress-title">{{ progressLabel || tr('烧录总进度', 'Total Progress') }}</span>
        <span data-testid="job-state" class="state">{{ progressState || stateLabel(state) }}</span>
        <strong data-testid="total-progress-label">{{ totalPercent }}%</strong>
      </div>
      <progress data-testid="total-progress" :value="totalProgress" max="1" :aria-label="tr('烧录总进度', 'Total flash progress')" />
    </div>
    <span v-if="stopping" class="waiting">{{ tr('等待探针安全停止', 'Waiting for the probe to stop safely') }}</span>
    <div class="job-actions">
      <button data-testid="start-job" :disabled="!canStart || confirming" class="primary" @click="$emit('start')">
        <Play :size="14" aria-hidden="true" />
        {{ tr('开始烧录', 'Start Flashing') }}
      </button>
      <button data-testid="stop-job" :disabled="!active || stopping" class="stop" @click="$emit('stop')">
        <Square :size="13" aria-hidden="true" />
        {{ tr('停止', 'Stop') }}
      </button>
    </div>
  </div>
</template>
<style scoped>
.action-bar{display:flex;flex-wrap:wrap;max-width:100%;box-sizing:border-box;align-items:center;gap:12px;padding:10px 12px;border-top:1px solid var(--of-border);background:var(--surface-muted);font-size:10px}.action-choices{display:flex;flex-wrap:wrap;gap:7px}.action-choices label{display:flex;align-items:center;gap:3px;color:var(--of-muted)}.progress-block{display:grid;flex:1 1 220px;min-width:180px;max-width:360px;gap:5px;margin-left:auto}.progress-meta{display:grid;grid-template-columns:auto minmax(58px,1fr) auto;align-items:center;gap:8px}.progress-title{color:var(--of-muted)}.progress-meta strong{color:var(--of-text);font-variant-numeric:tabular-nums}.progress-block progress{width:100%;height:7px;accent-color:var(--brand)}.progress-block progress::-webkit-progress-bar{border-radius:3px;background:var(--app-bg)}.progress-block progress::-webkit-progress-value{border-radius:3px;background:var(--brand)}.state{overflow:hidden;color:var(--of-accent);font-weight:700;text-overflow:ellipsis;white-space:nowrap}.waiting{flex-basis:100%;color:var(--of-warn);text-align:right}.job-actions{display:flex;gap:7px}.job-actions button{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:32px;padding:7px 10px;border:1px solid var(--control-border);border-radius:5px;background:var(--of-input);color:var(--of-text)}button.primary{border-color:var(--brand);background:var(--brand);color:#fff}button.stop{color:var(--of-danger)}button:disabled{opacity:.4}@media(max-width:720px){.progress-block{order:3;max-width:none;margin-left:0}.job-actions{margin-left:auto}}
.action-choices label.unavailable{opacity:.45;cursor:not-allowed}
</style>
