<template>
  <div class="dash-root">
    <div
      class="card"
      :class="{
        'card-full': tab === 'rtt' || tab === 'superwatch' || tab === 'vofa',
        'card-rtt': tab === 'rtt',
        'card-systemview': tab === 'systemview',
      }"
    >
      <div class="card-title-row">
        <div class="card-title">仪表盘</div>
        <div class="title-right">
          <span v-if="!deviceStatus.connected" class="device-link" @click="goConnect">
            设备未连接，点击连接
          </span>
          <span v-else-if="bridgeOwner" class="resource-status-inline">
            <span class="status-dot" :class="bridgeOwner.startsWith('ai:') ? 'dot-ai' : 'dot-user'"></span>
            <span v-if="bridgeOwner.startsWith('ai:')">AI 正在使用设备</span>
            <span v-else>{{ bridgeOwnerLabel }}</span>
          </span>
        </div>
      </div>
      <div class="tabs-bar">
        <button :class="['tab-btn', { active: tab === 'rtt' }]" @click="tab = 'rtt'">RTT View</button>
        <button :class="['tab-btn', { active: tab === 'flash' }]" @click="tab = 'flash'">烧录</button>
        <button :class="['tab-btn', { active: tab === 'debug' }]" @click="tab = 'debug'">调试控制</button>
        <button :class="['tab-btn', { active: tab === 'hardfault' }]" @click="tab = 'hardfault'">HardFault</button>
        <button :class="['tab-btn', { active: tab === 'symbols' }]" @click="tab = 'symbols'">符号表</button>
        <button :class="['tab-btn', { active: tab === 'memory' }]" @click="tab = 'memory'">内存</button>
        <button :class="['tab-btn', { active: tab === 'superwatch' }]" @click="tab = 'superwatch'">SuperWatch</button>
        <button :class="['tab-btn', { active: tab === 'serial' }]" @click="tab = 'serial'">串口监控</button>
        <button :class="['tab-btn', { active: tab === 'modbus' }]" @click="tab = 'modbus'">Modbus</button>
        <button :class="['tab-btn', { active: tab === 'vofa' }]" @click="tab = 'vofa'">VOFA+</button>
        <button :class="['tab-btn', { active: tab === 'systemview' }]" @click="tab = 'systemview'">RTOS Trace</button>
      </div>

      <RttViewTab v-show="tab === 'rtt'" :device-connected="deviceStatus.connected" />

      <!-- 烧录 -->
      <div v-if="tab === 'flash'">
        <div v-if="!deviceStatus.connected" class="alert alert-warn">请先连接设备。</div>
        <template v-else>
          <div class="form-row">
            <span class="form-label">固件文件</span>
            <input class="form-input" v-model="flashReq.firmware" placeholder=".hex 或 .bin 文件路径" />
          </div>
          <div class="form-row">
            <span class="form-label">烧录后校验</span>
            <label style="font-size:13px"><input type="checkbox" v-model="flashReq.verify" /> 启用</label>
          </div>
          <div class="form-row">
            <span class="form-label">烧录后复位</span>
            <label style="font-size:13px"><input type="checkbox" v-model="flashReq.reset_after" /> 启用</label>
          </div>
          <div class="form-row">
            <span class="form-label"></span>
            <button class="btn btn-primary" @click="doFlash" :disabled="flashing">
              {{ flashing ? '烧录中...' : '烧录固件' }}
            </button>
          </div>
        </template>
      </div>

      <!-- 调试控制 -->
      <div v-if="tab === 'debug'">
        <div v-if="!deviceStatus.connected" class="alert alert-warn">请先连接设备。</div>
        <template v-else>
          <div class="btn-group">
            <button class="btn" @click="doHalt">暂停 CPU</button>
            <button class="btn" @click="doResume">恢复 CPU</button>
            <button class="btn" @click="doReset">复位</button>
            <button class="btn btn-danger" @click="doErase">整片擦除</button>
          </div>
        </template>
      </div>

      <HardFaultTab v-if="tab === 'hardfault'" :device-connected="deviceStatus.connected" />
      <SymbolsTab v-if="tab === 'symbols'" :device-connected="deviceStatus.connected" />
      <MemoryTab v-if="tab === 'memory'" :device-connected="deviceStatus.connected" />
      <SuperWatchTab v-if="tab === 'superwatch'" :device-connected="deviceStatus.connected" />
      <SerialMonitorTab v-show="tab === 'serial'" :device-connected="deviceStatus.connected" />
      <ModbusTab v-show="tab === 'modbus'" :device-connected="deviceStatus.connected" />
      <VofaTab v-if="tab === 'vofa'" :device-connected="deviceStatus.connected" />
      <SystemViewTab v-show="tab === 'systemview'" :device-connected="deviceStatus.connected" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useMklinkApi } from '../composables/useMklinkApi'
import { useToast } from '../composables/useToast'
import { useResourceStatus } from '../composables/useResourceStatus'
import RttViewTab from '../components/dash/RttViewTab.vue'
import HardFaultTab from '../components/dash/HardFaultTab.vue'
import SymbolsTab from '../components/dash/SymbolsTab.vue'
import MemoryTab from '../components/dash/MemoryTab.vue'
import SuperWatchTab from '../components/dash/SuperWatchTab.vue'
import SerialMonitorTab from '../components/dash/SerialMonitorTab.vue'
import ModbusTab from '../components/dash/ModbusTab.vue'
import VofaTab from '../components/dash/VofaTab.vue'
import SystemViewTab from '../components/dash/SystemViewTab.vue'

const router = useRouter()
const {
  deviceStatus,
  flashDevice,
  resetDevice,
  eraseDevice,
  haltDevice,
  resumeDevice,
} = useMklinkApi()
const toast = useToast()
const { refresh: refreshResource, getBridgeOwner } = useResourceStatus()
const tab = ref('rtt')
const flashing = ref(false)
const flashReq = reactive({ firmware: '', verify: true, reset_after: true })

const bridgeOwner = computed(() => getBridgeOwner())
const bridgeOwnerLabel = computed(() => {
  const owner = bridgeOwner.value
  if (!owner) return ''
  const dashNames: Record<string, string> = {
    'user:dashboard:rtt': 'RTT View',
    'user:dashboard:superwatch': 'SuperWatch',
    'user:dashboard:vofa': 'VOFA+',
    'user:dashboard:systemview': 'RTOS Trace',
  }
  return dashNames[owner] || owner
})

// 周期性刷新资源状态
refreshResource()
setInterval(refreshResource, 3000)

function goConnect() {
  router.push({ name: 'config' })
}

async function doFlash() {
  flashing.value = true
  try { const r = await flashDevice(flashReq); toast.success('烧录完成: ' + JSON.stringify(r)) }
  catch (e: any) { toast.error('烧录失败: ' + e.message) }
  finally { flashing.value = false }
}
async function doReset() {
  if (!confirm('确定要复位 CPU？')) return
  try { await resetDevice(); toast.success('已复位') } catch (e: any) { toast.error(e.message) }
}
async function doErase() {
  if (!confirm('确定要整片擦除？此操作不可撤销。')) return
  try { await eraseDevice(); toast.success('整片擦除完成') } catch (e: any) { toast.error(e.message) }
}
async function doHalt() {
  if (!confirm('确定要暂停 CPU？')) return
  try { await haltDevice(); toast.info('CPU 已暂停') } catch (e: any) { toast.error(e.message) }
}
async function doResume() { try { await resumeDevice(); toast.success('CPU 已恢复') } catch (e: any) { toast.error(e.message) } }
</script>

<style scoped>
.dash-root {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.card-full {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding-bottom: 0;
  overflow: hidden;
  min-height: 0;
}
.card-full :deep(.waveform-viewer) {
  flex: 1;
  min-height: 0;
}
.card-full :deep(.rtt-view-tab) {
  flex: 1;
  min-height: 0;
  min-width: 0;
}
.card-rtt {
  padding-bottom: 16px;
}
.card-systemview {
  flex: 1 1 auto;
  min-height: 0;
  max-height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-gutter: stable;
  padding-bottom: 16px;
}
.card-systemview :deep(.sv-tab) {
  height: auto;
  min-height: 0;
}
.card-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-subtle);
}
.title-right {
  display: flex; align-items: center; gap: 8px;
}
.resource-status-inline {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--muted);
}
.device-link {
  font-size: 12px;
  color: var(--accent);
  cursor: pointer;
  text-decoration: none;
}
.device-link:hover { text-decoration: underline; }
.status-dot {
  width: 8px; height: 8px; border-radius: 50%; display: inline-block;
}
.dot-user { background: var(--success); }
.dot-ai { background: var(--warn); }
.alert-warn { color: var(--warn); padding: 8px; border: 1px solid var(--border); border-radius: var(--radius); background: #f5f0e1; }
</style>
