<template>
  <div class="modbus-workbench">
    <SetupHint v-if="portsLoaded && !ports.length" kind="info"
      :message="tr('未检测到可用串口。Modbus 不依赖 MKLink 设备连接。', 'No serial ports detected. Modbus does not depend on the MKLink device connection.')"
      :primary-label="tr('刷新串口', 'Refresh Ports')" :busy="refreshingPorts" @primary="refreshPorts" />

    <header class="workbench-hero">
      <div class="hero-copy">
        <span class="eyebrow">INDUSTRIAL SERIAL TOOL</span>
        <h2>{{ tr('Modbus RTU 工作台', 'Modbus RTU Workbench') }}</h2>
        <p>{{ tr('构建请求、解析寄存器，并检查每一帧真实串口数据。', 'Build requests, inspect registers, and trace every serial frame.') }}</p>
      </div>
      <div class="hero-status">
        <span class="protocol-tag">RTU</span>
        <span class="protocol-tag">CRC-16</span>
        <span class="state-pill" :class="{ online: running }"><i></i>{{ running ? tr('链路在线', 'Link online') : tr('链路离线', 'Link offline') }}</span>
      </div>
    </header>

    <section class="workbench-card connection-card">
      <div class="card-heading">
        <div class="title-stack"><span class="step-index">01</span><div><strong>{{ tr('串口连接', 'Serial connection') }}</strong><small>{{ tr('设置物理链路与从站参数', 'Configure the physical link and slave') }}</small></div></div>
        <div class="connection-summary mono"><span>{{ settings.port || '—' }}</span><b>·</b><span>{{ settings.baudrate }}</span><b>·</b><span>{{ settings.bytesize }}{{ settings.parity }}{{ settings.stopbits }}</span></div>
      </div>
      <div class="connection-layout">
        <div class="config-group primary-config">
          <span class="group-label">{{ tr('链路', 'LINK') }}</span>
          <div class="link-grid">
            <label class="port-field"><span>{{ tr('串口', 'Port') }}</span><select v-model="settings.port" class="form-input"><option v-for="p in ports" :key="p.device" :value="p.device">{{ p.device }} {{ p.description ? `· ${p.description}` : '' }}</option></select></label>
            <label><span>{{ tr('波特率', 'Baud') }}</span><input v-model.number="settings.baudrate" type="number" class="form-input mono" min="300" max="4000000" /></label>
          </div>
        </div>
        <div class="config-group serial-config">
          <span class="group-label">{{ tr('帧格式', 'FRAME') }}</span>
          <div class="serial-grid">
            <label><span>{{ tr('数据位', 'Data') }}</span><select v-model.number="settings.bytesize" class="form-input"><option :value="8">8 bit</option><option :value="7">7 bit</option></select></label>
            <label><span>{{ tr('校验', 'Parity') }}</span><select v-model="settings.parity" class="form-input"><option value="N">None</option><option value="E">Even</option><option value="O">Odd</option></select></label>
            <label><span>{{ tr('停止位', 'Stop') }}</span><select v-model.number="settings.stopbits" class="form-input"><option :value="1">1 bit</option><option :value="2">2 bit</option></select></label>
          </div>
        </div>
        <div class="config-group protocol-config">
          <span class="group-label">{{ tr('协议', 'PROTOCOL') }}</span>
          <div class="protocol-grid">
            <label><span>{{ tr('从站地址', 'Slave') }}</span><input v-model.number="settings.slave" type="number" class="form-input mono" min="1" max="247" /></label>
            <label><span>{{ tr('超时', 'Timeout') }}</span><div class="input-unit"><input v-model.number="settings.timeout" type="number" class="form-input mono" min="0.05" max="10" step="0.05" /><span>s</span></div></label>
            <label><span>{{ tr('重试', 'Retries') }}</span><input v-model.number="settings.retries" type="number" class="form-input mono" min="0" max="5" /></label>
          </div>
        </div>
        <div class="connect-block">
          <label class="echo-toggle"><input v-model="settings.localEcho" type="checkbox" /><span><b>{{ tr('本地回显', 'Local echo') }}</b><small>{{ tr('适配器回显兼容', 'Adapter compatibility') }}</small></span></label>
          <button v-if="!running" class="btn btn-primary connection-action" :disabled="!settings.port || connecting" @click="connect">{{ connecting ? tr('连接中…', 'Connecting…') : tr('连接', 'Connect') }}</button>
          <button v-else class="btn btn-danger connection-action" @click="disconnect">{{ tr('断开连接', 'Disconnect') }}</button>
        </div>
      </div>
    </section>

    <div class="workbench-columns">
      <section class="workbench-card request-card">
        <div class="card-heading">
          <div class="title-stack"><span class="step-index">02</span><div><strong>{{ tr('请求编辑器', 'Request builder') }}</strong><small>{{ tr('选择功能码并定义数据范围', 'Select a function and data range') }}</small></div></div>
          <span class="mode-badge">MASTER REQUEST</span>
        </div>
        <div class="request-grid">
          <label class="wide function-field"><span>{{ tr('功能码', 'Function') }}</span><select v-model.number="settings.fc" class="form-input"><option v-for="item in FUNCTION_OPTIONS" :key="item.fc" :value="item.fc">FC{{ String(item.fc).padStart(2, '0') }} · {{ tr(item.zh, item.en) }}</option></select></label>
          <label><span>{{ tr('起始地址', 'Start address') }}</span><input v-model="settings.start" class="form-input mono" placeholder="0 / 0x0000" /></label>
          <label v-if="isRead"><span>{{ tr('数量', 'Quantity') }}</span><input v-model.number="settings.quantity" type="number" class="form-input" min="1" :max="maxQuantity" /></label>
          <label v-else class="wide"><span>{{ tr('写入值', 'Write values') }}</span><textarea v-model="settings.values" class="form-input mono value-input" :placeholder="isBit ? '0, 1, ON, OFF' : '1, 2, 0x1234'" /></label>
        </div>
        <div class="request-preview mono"><span>SLAVE <b>{{ String(settings.slave).padStart(3, '0') }}</b></span><span>FC <b>{{ String(settings.fc).padStart(2, '0') }}</b></span><span>{{ tr('起始', 'START') }} <b>{{ settings.start || '0' }}</b></span><span v-if="isRead">{{ tr('数量', 'QTY') }} <b>{{ settings.quantity }}</b></span></div>
        <div class="request-actions">
          <button class="btn btn-primary send-button" :disabled="!running || sending" @click="sendOnce">{{ sending ? tr('发送中…', 'Sending…') : tr('发送请求', 'Send request') }}<span>→</span></button>
          <div class="loop-controls">
            <label><span>{{ tr('间隔(ms)', 'Interval (ms)') }}</span><input v-model.number="settings.loopIntervalMs" type="number" class="form-input" min="20" /></label>
            <label><span>{{ tr('次数 (0=连续)', 'Count (0=continuous)') }}</span><input v-model.number="settings.loopCount" type="number" class="form-input" min="0" max="100000" /></label>
            <button v-if="!loopRunning" class="btn" :disabled="!running" @click="startLoop">{{ tr('循环发送', 'Start Loop') }}</button>
            <button v-else class="btn btn-danger" @click="stopLoop">{{ tr('停止循环', 'Stop Loop') }}</button>
          </div>
        </div>
        <div v-if="loopStatus.completed || loopRunning" class="loop-summary">{{ tr('已执行', 'Completed') }} {{ loopStatus.completed || 0 }} · {{ tr('错误', 'Errors') }} {{ loopStatus.errors || 0 }}</div>
      </section>

      <section class="workbench-card response-card">
        <div class="card-heading">
          <div class="title-stack"><span class="step-index">03</span><div><strong>{{ tr('响应数据', 'Response data') }}</strong><small>{{ tr('寄存器值与类型转换', 'Register values and type conversion') }}</small></div></div>
          <span v-if="lastResult" class="duration-badge"><b>{{ lastResult.duration_ms }}</b> ms</span>
        </div>
        <div v-if="lastResult" class="result-meta mono"><span>ID {{ lastResult.id }}</span><span>FC{{ String(lastResult.fc).padStart(2, '0') }}</span><span>{{ tr('地址', 'address') }} {{ formatAddress(lastResult.start) }}</span></div>
        <div v-if="lastResult?.values?.length" class="result-table-wrap"><table class="result-table"><thead><tr><th>{{ tr('地址', 'Address') }}</th><th>HEX</th><th>DEC</th><th>{{ isResultBit ? tr('状态', 'State') : 'INT16' }}</th></tr></thead><tbody><tr v-for="(value, index) in lastResult.values" :key="index"><td>{{ formatAddress(lastResult.start + index) }}</td><td class="mono">{{ formatHex(value) }}</td><td>{{ formatDecimal(value) }}</td><td>{{ formatSigned(value) }}</td></tr></tbody></table></div>
        <div v-else class="empty-state response-empty"><span class="empty-glyph mono">01 03</span><strong>{{ tr('等待响应数据', 'Waiting for response') }}</strong><small>{{ tr('连接串口并发送请求后，解析结果会显示在这里。', 'Open the port and send a request to inspect parsed values.') }}</small></div>
      </section>
    </div>

    <section class="workbench-card log-card">
      <div class="card-heading log-heading"><div class="title-stack"><span class="terminal-lights"><i></i><i></i><i></i></span><div><strong>{{ tr('RTU 帧监视器', 'RTU frame monitor') }}</strong><small>{{ tr('实际串口收发数据 · CRC 自动校验', 'Raw serial traffic · automatic CRC validation') }}</small></div></div><div class="log-toolbar"><span class="frame-counter mono">{{ logs.length }} FRAMES</span><div class="log-actions"><button class="btn btn-sm" :class="{ active: logPaused }" @click="logPaused = !logPaused">{{ logPaused ? tr('继续', 'Resume') : tr('暂停', 'Pause') }}</button><button class="btn btn-sm" @click="logs = []">{{ tr('清空', 'Clear') }}</button><button class="btn btn-sm" :disabled="!logs.length" @click="exportLogs">{{ tr('导出', 'Export') }}</button></div></div></div>
      <div ref="logPanel" class="frame-log"><div v-for="(entry, index) in logs" :key="`${entry.timestamp}-${index}`" class="frame-row" :class="entry.direction"><span class="frame-time">{{ formatTime(entry.timestamp) }}</span><span class="frame-direction">{{ (entry.direction || entry.event).toUpperCase() }}</span><span class="frame-body mono">{{ entry.hex || entry.message || summarizeEvent(entry) }}</span><span v-if="entry.crc_ok !== undefined && entry.crc_ok !== null" class="crc" :class="entry.crc_ok ? 'ok' : 'bad'">CRC {{ entry.crc_ok ? 'OK' : 'ERR' }}</span></div><div v-if="!logs.length" class="empty-state">{{ tr('连接后发送请求，TX/RX 原始帧会显示在这里。', 'Connect and send a request to view raw TX/RX frames.') }}</div></div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useMklinkApi } from '../../composables/useMklinkApi'
import { useToast } from '../../composables/useToast'
import type { PortInfo } from '../../types/mklink'
import { tr } from '../../composables/useLanguage'
import SetupHint from './SetupHint.vue'
import { API_BASE } from '../../lib/runtimeEndpoint'
import { BIT_FUNCTIONS, buildTransaction, DEFAULT_MODBUS_SETTINGS, FUNCTION_OPTIONS, loadModbusSettings, MODBUS_SETTINGS_KEY, READ_FUNCTIONS } from '../../lib/modbusWorkbench'

interface TransactionResult { id: number; fc: number; start: number; values: Array<number | boolean>; duration_ms: number }
interface LogEntry { event: string; timestamp: number; direction?: string; hex?: string; crc_ok?: boolean | null; message?: string; [key: string]: unknown }

const toast = useToast()
const { listPorts: fetchPorts } = useMklinkApi()
const stored = loadModbusSettings(typeof localStorage === 'undefined' ? null : localStorage)
const settings = reactive({ ...DEFAULT_MODBUS_SETTINGS, ...stored })
const ports = ref<PortInfo[]>([])
const portsLoaded = ref(false), refreshingPorts = ref(false), connecting = ref(false), sending = ref(false)
const running = ref(false), loopRunning = ref(false), logPaused = ref(false)
const loopStatus = ref<Record<string, number>>({ completed: 0, errors: 0 })
const lastResult = ref<TransactionResult | null>(null)
const logs = ref<LogEntry[]>([])
const logPanel = ref<HTMLElement | null>(null)
let es: EventSource | null = null

const isRead = computed(() => READ_FUNCTIONS.has(Number(settings.fc)))
const isBit = computed(() => BIT_FUNCTIONS.has(Number(settings.fc)))
const isResultBit = computed(() => lastResult.value ? BIT_FUNCTIONS.has(Number(lastResult.value.fc)) : false)
const maxQuantity = computed(() => [1, 2].includes(Number(settings.fc)) ? 2000 : 125)

watch(settings, value => { if (typeof localStorage !== 'undefined') localStorage.setItem(MODBUS_SETTINGS_KEY, JSON.stringify(value)) }, { deep: true })

async function api(path: string, body?: unknown) {
  const response = await fetch(`${API_BASE}${path}`, body === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const payload = await response.json().catch(() => null)
  if (!response.ok) { const detail = payload?.detail; throw new Error(typeof detail === 'string' ? detail : detail?.conflict || response.statusText) }
  return payload
}
async function refreshPorts() { refreshingPorts.value = true; try { ports.value = await fetchPorts(); if (!ports.value.some(item => item.device === settings.port)) settings.port = ports.value[0]?.device || '' } catch (error) { toast.error(String(error)) } finally { refreshingPorts.value = false; portsLoaded.value = true } }
async function restoreStatus() { try { const status = await api('/api/dash/modbus/status'); running.value = Boolean(status.running); loopRunning.value = Boolean(status.loop?.running); loopStatus.value = running.value ? (status.loop || loopStatus.value) : { completed: 0, errors: 0 }; if (running.value) connectSSE() } catch { /* backend may still be starting */ } }
async function connect() { connecting.value = true; try { await api('/api/dash/modbus/start', { port: settings.port, slave: settings.slave, baudrate: settings.baudrate, bytesize: settings.bytesize, parity: settings.parity, stopbits: settings.stopbits, timeout: settings.timeout, retries: settings.retries, local_echo: settings.localEcho, registers: [], interval: Math.max(0.02, settings.loopIntervalMs / 1000) }); running.value = true; connectSSE(); toast.success(tr('Modbus 已连接', 'Modbus connected')) } catch (error) { toast.error(tr('连接失败: ', 'Connection failed: ') + String(error)) } finally { connecting.value = false } }
async function disconnect() { stopEventSource(); try { await api('/api/dash/modbus/stop', {}) } catch { /* already stopped */ } running.value = false; loopRunning.value = false; loopStatus.value = { completed: 0, errors: 0 }; toast.info(tr('Modbus 已断开', 'Modbus disconnected')) }
function requestPayload() { return buildTransaction(settings) }
async function sendOnce() { sending.value = true; try { lastResult.value = await api('/api/dash/modbus/transaction', requestPayload()) } catch (error) { toast.error(tr('请求失败: ', 'Request failed: ') + String(error)) } finally { sending.value = false } }
async function startLoop() { try { loopStatus.value = await api('/api/dash/modbus/loop/start', { ...requestPayload(), interval: settings.loopIntervalMs / 1000, count: settings.loopCount }); loopRunning.value = true } catch (error) { toast.error(tr('循环启动失败: ', 'Loop start failed: ') + String(error)) } }
async function stopLoop() { try { loopStatus.value = await api('/api/dash/modbus/loop/stop', {}) } catch { /* session stopped */ } loopRunning.value = false }
function connectSSE() { stopEventSource(); es = new EventSource(`${API_BASE}/api/dash/modbus/stream`); es.onmessage = event => { try { const value = JSON.parse(event.data); if (value.event === 'transaction') lastResult.value = value; if (value.event === 'loop') { loopRunning.value = Boolean(value.running); loopStatus.value = value }; if (value.event === 'stopped') { running.value = false; loopRunning.value = false }; if (value.event === 'error') toast.error(value.message); if (value.event === 'history') { if (!logPaused.value) logs.value = [...logs.value, ...(value.points || []).filter((item: LogEntry) => ['frame', 'error'].includes(item.event))].slice(-500) } else if (!logPaused.value && ['frame', 'error'].includes(value.event)) logs.value = [...logs.value, value].slice(-500); nextTick(() => { if (logPanel.value) logPanel.value.scrollTop = logPanel.value.scrollHeight }) } catch { /* malformed event */ } } }
function stopEventSource() { if (es) { es.close(); es = null } }
function formatAddress(value: number) { return `${value} / 0x${value.toString(16).toUpperCase().padStart(4, '0')}` }
function formatHex(value: number | boolean) { const n = typeof value === 'boolean' ? Number(value) : value; return `0x${n.toString(16).toUpperCase().padStart(4, '0')}` }
function formatDecimal(value: number | boolean) { return typeof value === 'boolean' ? (value ? '1' : '0') : String(value) }
function formatSigned(value: number | boolean) { if (typeof value === 'boolean') return value ? 'ON' : 'OFF'; return String(value >= 0x8000 ? value - 0x10000 : value) }
function formatTime(timestamp: number) { const date = new Date(timestamp * 1000); return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}.${String(date.getMilliseconds()).padStart(3, '0')}` }
function summarizeEvent(entry: LogEntry) { return entry.event === 'error' ? entry.message || '' : JSON.stringify(entry) }
function exportLogs() { const content = logs.value.map(entry => `${formatTime(entry.timestamp)} ${(entry.direction || entry.event).toUpperCase()} ${entry.hex || entry.message || summarizeEvent(entry)}`).join('\n'); const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = `mklink-modbus-${Date.now()}.log`; link.click(); URL.revokeObjectURL(url) }
onMounted(async () => { await refreshPorts(); await restoreStatus() })
onUnmounted(stopEventSource)
</script>

<style scoped>
.modbus-workbench{display:grid;gap:14px;max-width:1480px;margin:0 auto;padding:2px}.workbench-hero{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding:10px 4px 4px}.hero-copy{display:grid;gap:4px}.eyebrow{color:var(--accent);font:700 10px/1 var(--font-mono);letter-spacing:.16em}.hero-copy h2{margin:0;color:var(--fg);font-size:22px;letter-spacing:-.02em}.hero-copy p{margin:0;color:var(--muted);font-size:12px}.hero-status{display:flex;align-items:center;gap:7px}.protocol-tag,.mode-badge{padding:5px 8px;border:1px solid var(--border);border-radius:4px;background:color-mix(in srgb,var(--bg) 72%,transparent);color:var(--muted);font:700 10px/1 var(--font-mono);letter-spacing:.08em}.state-pill{display:inline-flex;align-items:center;gap:7px;margin-left:4px;padding:6px 10px;border:1px solid var(--border);border-radius:999px;background:var(--surface);color:var(--muted);font-size:11px;font-weight:600}.state-pill i{width:7px;height:7px;border-radius:50%;background:var(--dim)}.state-pill.online{color:var(--success);border-color:color-mix(in srgb,var(--success) 35%,var(--border));background:color-mix(in srgb,var(--success) 7%,var(--surface))}.state-pill.online i{background:var(--success);box-shadow:0 0 0 4px color-mix(in srgb,var(--success) 13%,transparent)}.workbench-card{min-width:0;overflow:hidden;border:1px solid var(--border);border-radius:8px;background:var(--surface);box-shadow:0 1px 2px rgb(25 28 30 / 3%)}.card-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:48px;padding:10px 14px;border-bottom:1px solid var(--border-subtle);background:color-mix(in srgb,var(--surface) 94%,var(--bg))}.title-stack{display:flex;align-items:center;gap:10px}.title-stack>div{display:grid;gap:2px}.title-stack strong{font-size:13px}.title-stack small{color:var(--muted);font-size:10px}.step-index{display:grid;place-items:center;width:25px;height:25px;border:1px solid color-mix(in srgb,var(--accent) 30%,var(--border));border-radius:5px;background:color-mix(in srgb,var(--accent) 7%,var(--surface));color:var(--accent);font:700 10px var(--font-mono)}.connection-summary{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:10px}.connection-summary b{color:var(--border)}.connection-layout{display:grid;grid-template-columns:minmax(260px,1.5fr) minmax(250px,1fr) minmax(270px,1fr) minmax(150px,.65fr);align-items:stretch}.config-group,.connect-block{padding:13px 14px}.config-group+.config-group,.connect-block{border-left:1px solid var(--border-subtle)}.group-label{display:block;margin-bottom:8px;color:var(--dim);font:700 9px/1 var(--font-mono);letter-spacing:.13em}.link-grid{display:grid;grid-template-columns:minmax(160px,1.6fr) minmax(100px,.8fr);gap:9px}.serial-grid,.protocol-grid{display:grid;grid-template-columns:repeat(3,minmax(65px,1fr));gap:8px}label{display:grid;min-width:0;gap:5px;color:var(--muted);font-size:10px}label>span{font-weight:600}.form-input{width:100%;min-width:0;height:34px;box-sizing:border-box;border-radius:5px!important}.input-unit{position:relative}.input-unit input{padding-right:24px}.input-unit>span{position:absolute;right:9px;top:50%;transform:translateY(-50%);color:var(--dim);font:10px var(--font-mono)}.connect-block{display:grid;gap:9px;align-content:end;background:color-mix(in srgb,var(--bg) 50%,var(--surface))}.echo-toggle{display:flex;align-items:center;gap:8px;cursor:pointer}.echo-toggle input{margin:0}.echo-toggle>span{display:grid;gap:1px}.echo-toggle b{color:var(--fg);font-size:10px}.echo-toggle small{color:var(--muted);font-size:9px;font-weight:400}.connection-action{display:flex;align-items:center;justify-content:center;gap:7px;height:34px;white-space:nowrap}.workbench-columns{display:grid;grid-template-columns:minmax(400px,.92fr) minmax(400px,1.08fr);gap:14px}.request-card,.response-card{min-height:340px}.request-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px}.wide{grid-column:1/-1}.function-field select{height:38px;font-weight:600;color:var(--fg)}.mono{font-family:var(--font-mono)}.value-input{min-height:70px;resize:vertical}.request-preview{display:flex;gap:18px;margin:0 14px;padding:8px 10px;border:1px dashed var(--border);border-radius:5px;background:var(--bg);color:var(--muted);font-size:9px;letter-spacing:.03em}.request-preview b{color:var(--fg);font-weight:600}.request-actions{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;padding:12px 14px 14px;flex-wrap:wrap}.send-button{display:flex;align-items:center;justify-content:space-between;gap:18px;min-width:128px}.send-button span{font-size:15px}.loop-controls{display:flex;align-items:flex-end;gap:7px;flex-wrap:wrap}.loop-controls label{width:108px}.loop-summary{margin:0 14px 13px;color:var(--muted);font:10px var(--font-mono)}.result-meta{display:flex;gap:8px;padding:10px 14px 0;color:var(--muted);font-size:10px}.result-meta span{padding:4px 7px;border-radius:4px;background:var(--bg)}.duration-badge{padding:5px 8px;border-radius:4px;background:color-mix(in srgb,var(--success) 9%,var(--surface));color:var(--success);font:10px var(--font-mono)}.duration-badge b{font-size:12px}.result-table-wrap{overflow:auto;max-height:280px;margin:10px 14px 14px;border:1px solid var(--border-subtle);border-radius:5px}.result-table{border-collapse:collapse;width:100%;font-size:11px}.result-table th,.result-table td{border-bottom:1px solid var(--border-subtle);text-align:left;padding:7px 9px}.result-table th{position:sticky;top:0;background:var(--bg);color:var(--muted);font:700 9px var(--font-mono);letter-spacing:.08em}.result-table tbody tr:hover{background:color-mix(in srgb,var(--accent) 4%,transparent)}.result-table tbody tr:last-child td{border-bottom:0}.empty-state{color:var(--muted);font-size:12px;padding:24px 10px;text-align:center}.response-empty{display:flex;min-height:230px;flex-direction:column;align-items:center;justify-content:center;gap:7px}.response-empty strong{color:var(--fg);font-size:12px}.response-empty small{max-width:300px;line-height:1.5}.empty-glyph{display:grid;place-items:center;width:62px;height:42px;margin-bottom:3px;border:1px solid var(--border);border-radius:7px;background:var(--bg);color:var(--dim);font-size:11px}.log-card{background:#151a1f;border-color:#252d34}.log-heading{background:#1b2127;border-color:#293139;color:#e7ebee}.log-heading .title-stack small{color:#87929d}.terminal-lights{display:flex;gap:5px}.terminal-lights i{width:7px;height:7px;border-radius:50%;background:#4f5963}.terminal-lights i:first-child{background:#d2675b}.terminal-lights i:nth-child(2){background:#d4a548}.terminal-lights i:last-child{background:#55a875}.log-toolbar,.log-actions{display:flex;align-items:center;gap:6px}.frame-counter{margin-right:5px;color:#707c87;font-size:9px}.log-actions .btn{border-color:#343e47;background:#20272e;color:#aeb7bf}.log-actions .btn:hover:not(:disabled),.log-actions .btn.active{background:#2a333c;color:#fff}.frame-log{min-height:180px;max-height:300px;overflow:auto;background:#11161a}.frame-row{display:grid;grid-template-columns:94px 38px minmax(0,1fr) 66px;gap:9px;align-items:center;padding:6px 12px;border-bottom:1px solid #20272d;color:#bdc5cc;font-size:11px}.frame-row:hover{background:#171e23}.frame-time{color:#626e78}.frame-direction{font-weight:800;letter-spacing:.05em}.frame-row.tx .frame-direction{color:#63a8e7}.frame-row.rx .frame-direction{color:#65bd8c}.frame-body{overflow-wrap:anywhere;color:#d4d9dd;letter-spacing:.015em}.crc{font:700 9px var(--font-mono)}.crc.ok{color:#65bd8c}.crc.bad{color:#e17268}.frame-log .empty-state{color:#65717b}.btn-sm{font-size:10px;padding:4px 8px}@media(max-width:1280px){.connection-layout{grid-template-columns:1fr 1fr}.protocol-config,.connect-block{border-top:1px solid var(--border-subtle)}.protocol-config{border-left:0!important}.workbench-columns{grid-template-columns:1fr 1fr}}@media(max-width:980px){.workbench-columns{grid-template-columns:1fr}}@media(max-width:700px){.workbench-hero{align-items:flex-start;flex-direction:column}.connection-layout{grid-template-columns:1fr}.config-group+.config-group,.connect-block{border-left:0;border-top:1px solid var(--border-subtle)}.hero-status{flex-wrap:wrap}.connection-summary{display:none}.link-grid,.serial-grid,.protocol-grid,.request-grid{grid-template-columns:1fr}.wide{grid-column:auto}.request-preview{flex-wrap:wrap;gap:9px}.request-actions{align-items:stretch;flex-direction:column}.send-button{width:100%}.loop-controls{display:grid;grid-template-columns:1fr 1fr}.loop-controls label{width:auto}.frame-row{grid-template-columns:76px 32px minmax(0,1fr)}.crc{display:none}.log-heading{align-items:flex-start;flex-direction:column}.log-toolbar{width:100%;justify-content:space-between}}
</style>
