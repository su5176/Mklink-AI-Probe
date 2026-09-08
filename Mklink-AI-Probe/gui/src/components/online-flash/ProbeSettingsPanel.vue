<script setup lang="ts">
import type { ProbeRecord } from '../../types/onlineFlash'
import { tr } from '../../composables/useLanguage'
import { useConfirmation } from '../../composables/useConfirmation'
const confirm = useConfirmation()

const voltageChoices = [1800, 3300, 5000] as const

const props = defineProps<{
  probes: ProbeRecord[]
  selectedId: string
  frequency: number
  connectMode: string
  resetMode: string
  resetVoltageMv: 1800 | 3300 | 5000
  busy: boolean
  error: string
}>()

const emit = defineEmits<{
  refresh: []
  'update:selectedId': [value: string]
  'update:frequency': [value: number]
  'update:connectMode': [value: string]
  'update:resetMode': [value: string]
  'update:resetVoltageMv': [value: 1800 | 3300 | 5000]
}>()

async function updateResetVoltage(value: 1800 | 3300 | 5000, input: HTMLInputElement): Promise<void> {
  if (value === 5000) {
    input.checked = false
    const previous = input.closest('.voltage-options')?.querySelector<HTMLInputElement>(`input[value="${props.resetVoltageMv}"]`)
    if (previous) previous.checked = true
    if (!await confirm(tr(
    '5V 可能永久损坏不耐受 5V 的目标板。仅在确认当前目标硬件支持 5V 时选择。确定选择 5V？',
    '5 V may permanently damage a target that is not 5 V tolerant. Select it only after verifying the connected hardware. Select 5 V?',
    ))) return
  }
  if (props.busy) return
  emit('update:resetVoltageMv', value)
}
</script>

<template>
  <section class="panel-block">
    <div class="panel-title"><span>{{ tr('设备接入', 'Probe Connection') }}</span><button :disabled="busy" @click="$emit('refresh')">{{ tr('刷新', 'Refresh') }}</button></div>
    <label>{{ tr('MKLink 探针', 'MKLink Probe') }}
      <select data-testid="probe-select" :value="selectedId" :disabled="busy" @change="$emit('update:selectedId', ($event.target as HTMLSelectElement).value)">
        <option value="">{{ tr('请选择探针', 'Select a probe') }}</option>
        <option v-for="probe in probes" :key="probe.unique_id" :value="probe.unique_id">
          {{ probe.product_name }} · {{ probe.serial_number || probe.unique_id }}
        </option>
      </select>
    </label>
    <p v-if="!probes.length" class="hint">{{ tr('未发现精确匹配的 MKLink CMSIS-DAP 探针', 'No exact MKLink CMSIS-DAP probe found') }}</p>
    <p v-if="error" class="error">{{ error }}</p>
  </section>
  <section class="panel-block">
    <h3>{{ tr('基本设置', 'Basic Settings') }}</h3>
    <label>{{ tr('SWD 频率', 'SWD Frequency') }}
      <select data-testid="frequency" :value="frequency" @change="$emit('update:frequency', Number(($event.target as HTMLSelectElement).value))">
        <option :value="1000000">1 MHz</option><option :value="2000000">2 MHz</option>
        <option :value="4000000">4 MHz</option><option :value="8000000">8 MHz</option>
        <option :value="10000000">10 MHz</option>
      </select>
    </label>
    <label>{{ tr('连接方式', 'Connection Mode') }}
      <select data-testid="connect-mode" :value="connectMode" @change="$emit('update:connectMode', ($event.target as HTMLSelectElement).value)">
        <option value="halt">{{ tr('连接后暂停', 'Halt after connect') }}</option><option value="attach">{{ tr('保持运行', 'Keep running') }}</option>
        <option value="under-reset">{{ tr('复位下连接', 'Connect under reset') }}</option>
      </select>
    </label>
    <label>{{ tr('复位方式', 'Reset Mode') }}
      <select data-testid="reset-mode" :value="resetMode" @change="$emit('update:resetMode', ($event.target as HTMLSelectElement).value)">
        <option value="default">{{ tr('默认', 'Default') }}</option><option value="hardware">{{ tr('硬件复位', 'Hardware reset') }}</option><option value="software">{{ tr('软件复位', 'Software reset') }}</option><option value="power-cycle">{{ tr('断电复位', 'Power-cycle reset') }}</option>
      </select>
    </label>
    <div v-if="resetMode === 'power-cycle'" class="voltage-setting" data-testid="reset-voltage-setting">
      <span>{{ tr('VCC 恢复电压（默认 3.3V）', 'VCC restore voltage (3.3 V default)') }}</span>
      <div class="voltage-options">
        <label v-for="choice in voltageChoices" :key="choice">
          <input :data-testid="`reset-voltage-${choice}`" type="radio" name="reset-voltage" :value="choice" :checked="resetVoltageMv === choice" @change="updateResetVoltage(choice, $event.target as HTMLInputElement)">
          {{ (choice / 1000).toFixed(choice === 5000 ? 0 : 1) }}V
        </label>
      </div>
      <p class="power-warning">{{ tr('执行断电复位时会关闭 VCC，等待 3 秒后按所选电压重新输出。', 'Power-cycle reset disables VCC, waits 3 seconds, then restores the selected voltage.') }}</p>
    </div>
  </section>
</template>

<style scoped>
.panel-block{padding:14px;border-bottom:1px solid var(--of-border)}.panel-title{display:flex;align-items:center;justify-content:space-between}h3,.panel-title{margin:0 0 10px;font-size:13px;color:var(--of-text)}label{display:grid;gap:5px;margin:9px 0;color:var(--of-muted);font-size:11px}select,button{border:1px solid var(--of-border);border-radius:5px;background:var(--of-input);color:var(--of-text);padding:7px;font:inherit}button{padding:4px 9px}.hint,.error{font-size:11px}.hint{color:var(--of-muted)}.error{color:var(--of-danger)}.voltage-setting{display:grid;gap:7px;margin:10px 0;color:var(--of-muted);font-size:11px}.voltage-options{display:flex;gap:14px}.voltage-options label{display:flex;align-items:center;gap:5px;margin:0}.power-warning{margin:0;color:var(--of-warn);line-height:1.45}
</style>
