<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useOfflineFlashApi } from '../composables/useOfflineFlashApi'
import { useOnlineFlashApi } from '../composables/useOnlineFlashApi'
import { tr } from '../composables/useLanguage'
import { listenForFirmwarePathDrops, pickFirmwareFiles } from '../lib/filePicker'
import type { TargetRecord } from '../types/onlineFlash'
import type {
  OfflineAlgorithmCandidate,
  OfflineAlgorithmConfig,
  OfflineConfigPayload,
  OfflineDiskStatus,
  OfflineFirmwareConfig,
  OfflinePreview,
} from '../types/offlineFlash'

interface AlgorithmRow extends Omit<OfflineAlgorithmCandidate, 'source_kind'> {
  source_kind: 'upload' | 'pack' | 'profile' | 'existing'
  file: File | null
}

interface FirmwareRow {
  id: string
  file: File | null
  source_path: string | null
  source_stamp: string
  file_name: string
  format: 'bin' | 'hex'
  base_address: string
  algorithm_id: string
}

type ProbeModel = 'V2' | 'V3' | 'V4'
defineOptions({ name: 'OfflineFlashView' })

const OFFLINE_STORAGE_KEY = 'mklink.offlineFlash.settings'
function savedSettings(): { model?: ProbeModel; scriptName?: string; automaticCount?: number; idcodeTimeout?: number; swdClock?: number } {
  try { return JSON.parse(localStorage.getItem(OFFLINE_STORAGE_KEY) || '{}') } catch { return {} }
}
const saved = savedSettings()

const hpmBoards = [
  'hpm5300evk', 'hpm5301evklite', 'hpm5e00evk', 'hpm6e00evk',
  'hpm6p00evk', 'hpm6200evk', 'hpm6300evk', 'hpm6750evk2',
  'hpm6750evkmini', 'hpm6800evk',
]
function isHpmPart(partNumber: string): boolean {
  return partNumber.trim().toLowerCase().startsWith('hpm')
}
function defaultHpmBoard(partNumber: string): string {
  const part = partNumber.trim().toLowerCase()
  const match = [
    ['hpm5301', 'hpm5301evklite'], ['hpm5300', 'hpm5300evk'],
    ['hpm5e', 'hpm5e00evk'], ['hpm6e', 'hpm6e00evk'],
    ['hpm6p', 'hpm6p00evk'], ['hpm6200', 'hpm6200evk'],
    ['hpm6300', 'hpm6300evk'], ['hpm6750', 'hpm6750evk2'],
    ['hpm6800', 'hpm6800evk'],
  ].find(([prefix]) => part.startsWith(prefix))
  return match?.[1] || ''
}

const offline = useOfflineFlashApi()
const online = useOnlineFlashApi()

const disk = ref<OfflineDiskStatus | null>(null)
const model = ref<ProbeModel | ''>(saved.model ?? '')
const scriptName = ref(saved.scriptName ?? 'factory-download.py')
const automaticCount = ref(saved.automaticCount ?? 1)
const idcodeTimeout = ref(saved.idcodeTimeout ?? 10000)
const swdClock = ref(saved.swdClock ?? 10000000)

const algorithms = ref<AlgorithmRow[]>([])
const firmwares = ref<FirmwareRow[]>([])
const targetQuery = ref('STM32F103RC')
const targetPart = ref('')
const hpmBoard = ref('')
const targets = ref<TargetRecord[]>([])
const targetBusy = ref(false)
const operationBusy = ref(false)
const error = ref('')
const errorTitle = ref('')
const errorDetail = ref('')
const notice = ref('')
const preview = ref<OfflinePreview | null>(null)
const triggerLines = ref<string[]>([])
const deployedScriptName = ref('')
const deployedModel = ref<'V2' | 'V3' | 'V4' | ''>('')
const firmwareDropActive = ref(false)
const replacementAlgorithmId = ref('')
const replacementFlmInput = ref<HTMLInputElement | null>(null)
let sourcePollTimer: ReturnType<typeof setTimeout> | null = null
let sourcePollingEnabled = false
let stopNativeDropListener: (() => void) | null = null

let sequence = 0
const nextId = (prefix: string) => `${prefix}-${++sequence}`

const effectiveModel = computed(() => model.value)
const effectiveScriptName = computed(() => (
  effectiveModel.value === 'V2' || effectiveModel.value === 'V3'
    ? 'offline_download.py'
    : scriptName.value
))
const scriptFieldName = computed({
  get: () => effectiveScriptName.value,
  set: value => { if (effectiveModel.value === 'V4') scriptName.value = value },
})
const hpmMode = computed(() => isHpmPart(targetPart.value))
const selectedAlgorithmIds = computed(() => new Set(
  firmwares.value.map(item => item.algorithm_id).filter(Boolean),
))
const selectedAlgorithms = computed(() => algorithms.value.filter(item => selectedAlgorithmIds.value.has(item.id)))
const unavailableSelectedAlgorithms = computed(() => selectedAlgorithms.value.filter(item => (
  item.source_kind === 'upload' ? !item.file : !item.available
)))
const selectionWarning = computed(() => unavailableSelectedAlgorithms.value.length
  ? tr(
    `烧录顺序使用的下载算法不可用：${unavailableSelectedAlgorithms.value.map(item => item.file_name).join('、')}。请选择本地 FLM 替换，或为固件选择其他算法。`,
    `The flash sequence uses unavailable algorithms: ${unavailableSelectedAlgorithms.value.map(item => item.file_name).join(', ')}. Choose a local FLM replacement or select another algorithm.`,
  )
  : '')
const canBuild = computed(() => (
  !!effectiveModel.value
  && !!disk.value?.available
  && firmwares.value.length > 0
  && (hpmMode.value
    ? !!hpmBoard.value && firmwares.value.every(item => (item.file || item.source_path) && item.format === 'bin' && !!item.base_address)
    : selectedAlgorithms.value.length > 0
      && unavailableSelectedAlgorithms.value.length === 0
      && firmwares.value.every(item => (item.file || item.source_path) && item.algorithm_id && algorithms.value.some(algorithm => algorithm.id === item.algorithm_id)))
))
const canTrigger = computed(() => (
  !!disk.value?.available
  && !!deployedScriptName.value
  && !!deployedModel.value
  && !operationBusy.value
))

watch(
  [model, scriptName, automaticCount, idcodeTimeout, swdClock, targetPart, hpmBoard, algorithms, firmwares],
  () => {
    preview.value = null
    deployedScriptName.value = ''
    deployedModel.value = ''
    triggerLines.value = []
  },
  { deep: true },
)

watch([model, scriptName, automaticCount, idcodeTimeout, swdClock], () => {
  localStorage.setItem(OFFLINE_STORAGE_KEY, JSON.stringify({
    model: model.value || undefined,
    scriptName: scriptName.value,
    automaticCount: automaticCount.value,
    idcodeTimeout: idcodeTimeout.value,
    swdClock: swdClock.value,
  }))
})

function message(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}

function setError(value: unknown): void {
  const raw = message(value)
  error.value = raw
  errorTitle.value = ''
  errorDetail.value = ''
  const missingFlm = raw.match(/existing FLM is missing:\s*(.+)$/i)
  if (missingFlm) {
    errorTitle.value = tr(`缺少下载算法 ${missingFlm[1]}`, `Missing flash algorithm ${missingFlm[1]}`)
    errorDetail.value = tr(
      `当前烧录顺序使用了这个 FLM，但下载器 U 盘中没有对应文件。请选择本地 FLM 替换，或为固件选择其他算法。`,
      `The current flash sequence uses this FLM, but it is not on the probe USB drive. Choose a local FLM replacement or select another algorithm for the firmware.`,
    )
    return
  }
  if (/MICROKEEN disk is unavailable/i.test(raw)) {
    errorTitle.value = tr('未找到脱机下载器 U 盘', 'Offline probe USB drive not found')
    errorDetail.value = tr('请插入下载器 U 盘后刷新状态，再重新部署。', 'Insert the probe USB drive, refresh its status, and deploy again.')
    return
  }
  if (/missing firmware source|firmware source is unavailable/i.test(raw)) {
    errorTitle.value = tr('固件文件不可用', 'Firmware source unavailable')
    errorDetail.value = tr('请重新选择或拖入对应的 BIN / HEX 文件。', 'Choose or drop the corresponding BIN / HEX file again.')
    return
  }
  if (/offline destination names must be unique/i.test(raw)) {
    errorTitle.value = tr('烧录目标文件名重复', 'Duplicate deployment file name')
    errorDetail.value = tr('请修改烧录顺序中的文件名，使每个目标文件名唯一。', 'Rename the files in the flash sequence so each deployment name is unique.')
  }
}

function clearError(): void {
  error.value = ''
  errorTitle.value = ''
  errorDetail.value = ''
}

function targetAction(target: TargetRecord): string {
  if (isHpmPart(target.part_number)) return tr('使用 ROM API', 'Use ROM API')
  return target.installed ? tr('加入算法', 'Add Algorithm') : tr('下载 Pack', 'Download Pack')
}

async function refreshDisk(): Promise<void> {
  try { disk.value = await offline.getStatus() }
  catch (value) { setError(value) }
}

function modelChanged(): void {
  preview.value = null
  if (effectiveModel.value === 'V2') automaticCount.value = 1
}

async function searchTargets(): Promise<void> {
  targetBusy.value = true
  clearError()
  try { targets.value = await online.searchTargets(targetQuery.value, { limit: 30 }) }
  catch (value) { setError(value) }
  finally { targetBusy.value = false }
}

function mergeAlgorithms(items: OfflineAlgorithmCandidate[]): void {
  for (const item of items) {
    const duplicate = algorithms.value.some(existing => (
      existing.file_name.toLowerCase() === item.file_name.toLowerCase()
      && existing.flash_base === item.flash_base
      && existing.ram_base === item.ram_base
      && (existing.source_token || existing.id) === (item.source_token || item.id)
    ))
    if (!duplicate) algorithms.value.push({ ...item, file: null })
  }
  if (algorithms.value.length === 1) {
    firmwares.value.forEach(item => { item.algorithm_id = algorithms.value[0].id })
  }
}

async function addTargetAlgorithms(target: TargetRecord): Promise<void> {
  targetBusy.value = true
  clearError()
  notice.value = ''
  try {
    targetPart.value = target.part_number
    if (isHpmPart(target.part_number)) {
      algorithms.value = []
      hpmBoard.value = defaultHpmBoard(target.part_number)
      firmwares.value.forEach(item => { item.algorithm_id = '' })
      notice.value = tr(`${target.part_number} 使用 HPM ROM API，无需 Pack 或 FLM`, `${target.part_number} uses HPM ROM API; no Pack or FLM required`)
      return
    }
    hpmBoard.value = ''
    if (!target.installed) await online.installPack(target.part_number)
    const items = await offline.listAlgorithms(target.part_number)
    if (!items.length) throw new Error(tr(`未找到 ${target.part_number} 的 FLM 算法`, `No FLM algorithm found for ${target.part_number}`))
    mergeAlgorithms(items)
    notice.value = tr(`已加入 ${items.length} 个 ${target.part_number} 算法候选`, `Added ${items.length} algorithm candidates for ${target.part_number}`)
  } catch (value) { setError(value) }
  finally { targetBusy.value = false }
}

function addManualFlm(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.flm')) {
    setError(new Error(tr('下载算法必须是 .FLM 文件', 'Flash algorithm must be an .FLM file')))
    return
  }
  const id = nextId('flm')
  algorithms.value.push({
    id,
    file_name: file.name,
    flash_base: '0x08000000',
    ram_base: '0x20000000',
    source_kind: 'upload',
    source_token: null,
    origin: '本地文件',
    available: true,
    on_probe: false,
    file,
  })
  if (algorithms.value.length === 1) {
    firmwares.value.forEach(item => { item.algorithm_id = id })
  }
}

function chooseReplacement(id: string): void {
  replacementAlgorithmId.value = id
  replacementFlmInput.value?.click()
}

function replaceAlgorithm(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const algorithm = algorithms.value.find(item => item.id === replacementAlgorithmId.value)
  replacementAlgorithmId.value = ''
  if (!file || !algorithm) return
  if (!file.name.toLowerCase().endsWith('.flm')) {
    setError(new Error(tr('下载算法必须是 .FLM 文件', 'Flash algorithm must be an .FLM file')))
    return
  }
  algorithm.source_kind = 'upload'
  algorithm.source_token = null
  algorithm.origin = tr('本地替换', 'Local replacement')
  algorithm.available = true
  algorithm.on_probe = false
  algorithm.file = file
  clearError()
  preview.value = null
}

function removeAlgorithm(index: number): void {
  const [removed] = algorithms.value.splice(index, 1)
  const fallback = algorithms.value[0]?.id || ''
  firmwares.value.forEach(item => {
    if (item.algorithm_id === removed.id) item.algorithm_id = fallback
  })
  preview.value = null
}

function addFirmwareSources(sources: Array<string | File>): void {
  for (const source of sources) {
    const path = typeof source === 'string' ? source : ''
    const file = source instanceof File ? source : null
    const name = file?.name || path.split(/[\\/]/).pop() || ''
    const suffix = name.split('.').pop()?.toLowerCase()
    if (suffix !== 'bin' && suffix !== 'hex') {
      setError(new Error(tr('固件只支持 BIN 或 HEX', 'Only BIN or HEX firmware is supported')))
      continue
    }
    if (hpmMode.value && suffix !== 'bin') {
      setError(new Error(tr('HPM ROM API 只支持 BIN 固件', 'HPM ROM API supports BIN firmware only')))
      continue
    }
    firmwares.value.push({
      id: nextId('firmware'),
      file,
      source_path: path || null,
      source_stamp: '',
      file_name: name,
      format: suffix,
      base_address: suffix === 'bin' ? (hpmMode.value ? '0x80000400' : '0x08000000') : '',
      algorithm_id: hpmMode.value ? '' : algorithms.value[0]?.id || '',
    })
  }
  preview.value = null
}

function addFirmware(event: Event): void {
  const input = event.target as HTMLInputElement
  addFirmwareSources(Array.from(input.files || []))
  input.value = ''
}

async function browseFirmware(): Promise<void> {
  addFirmwareSources(await pickFirmwareFiles(true))
}

function dropFirmware(event: DragEvent): void {
  firmwareDropActive.value = false
  addFirmwareSources(Array.from(event.dataTransfer?.files || []))
}

async function startNativeDrops(): Promise<void> {
  if (stopNativeDropListener) return
  stopNativeDropListener = await listenForFirmwarePathDrops(
    paths => addFirmwareSources(paths),
    active => { firmwareDropActive.value = active },
  )
}

function stopNativeDrops(): void {
  stopNativeDropListener?.()
  stopNativeDropListener = null
  firmwareDropActive.value = false
}

async function pollFirmwareSources(): Promise<void> {
  for (const item of firmwares.value) {
    if (!item.source_path) continue
    try {
      const status = await online.getImageSourceStatus(item.source_path)
      const stamp = `${status.size}:${status.mtime_ns}`
      if (item.source_stamp && stamp !== item.source_stamp) {
        preview.value = null
        deployedScriptName.value = ''
        deployedModel.value = ''
        notice.value = tr(`已自动加载重新编译的 ${status.file_name}`, `Automatically loaded rebuilt ${status.file_name}`)
      }
      item.source_stamp = stamp
    } catch (value) {
      setError(new Error(tr(`固件路径不可用：${message(value)}`, `Firmware path is unavailable: ${message(value)}`)))
    }
  }
}

function startSourcePolling(): void {
  if (sourcePollingEnabled) return
  sourcePollingEnabled = true
  const run = async () => {
    await pollFirmwareSources()
    if (sourcePollingEnabled) sourcePollTimer = setTimeout(run, 1000)
  }
  void run()
}

function stopSourcePolling(): void {
  sourcePollingEnabled = false
  if (sourcePollTimer !== null) clearTimeout(sourcePollTimer)
  sourcePollTimer = null
}

function moveFirmware(index: number, delta: number): void {
  const target = index + delta
  if (target < 0 || target >= firmwares.value.length) return
  const rows = [...firmwares.value]
  ;[rows[index], rows[target]] = [rows[target], rows[index]]
  firmwares.value = rows
  preview.value = null
}

function buildRequest(): {
  payload: OfflineConfigPayload
  firmwareFiles: File[]
  flmFiles: File[]
} {
  const flmFiles: File[] = []
  const algorithmPayload: OfflineAlgorithmConfig[] = algorithms.value
    .filter(item => selectedAlgorithmIds.value.has(item.id))
    .map(item => {
    let uploadIndex: number | null = null
    if (item.source_kind === 'upload') {
      if (!item.file) throw new Error(tr(`请选择 ${item.file_name} 的 FLM 文件`, `Select the FLM file for ${item.file_name}`))
      uploadIndex = flmFiles.push(item.file) - 1
    }
    return {
      id: item.id,
      file_name: item.file_name,
      flash_base: item.flash_base,
      ram_base: item.ram_base,
      source_kind: item.source_kind,
      source_token: item.source_token,
      upload_index: uploadIndex,
    }
    })
  const firmwareFiles: File[] = []
  const firmwarePayload: OfflineFirmwareConfig[] = firmwares.value.map(item => {
    const uploadIndex = item.file ? firmwareFiles.push(item.file) - 1 : null
    return {
      id: item.id,
      file_name: item.file_name,
      format: item.format,
      base_address: item.format === 'bin' ? item.base_address : null,
      algorithm_id: item.algorithm_id,
      upload_index: uploadIndex,
      source_path: item.source_path,
    }
  })
  if (!model.value) throw new Error(tr('请选择下载器型号', 'Select a probe model'))
  return {
    payload: {
      model: model.value,
      script_name: scriptName.value,
      auto_download_count: Number(automaticCount.value),
      wait_idcode_timeout_ms: Number(idcodeTimeout.value),
      swd_clock_hz: Number(swdClock.value),
      target_part: targetPart.value || null,
      board: hpmMode.value ? hpmBoard.value || null : null,
      algorithms: algorithmPayload,
      firmwares: firmwarePayload,
    },
    firmwareFiles,
    flmFiles,
  }
}

async function generatePreview(): Promise<void> {
  operationBusy.value = true
  clearError()
  notice.value = ''
  try {
    preview.value = await offline.preview(buildRequest().payload)
    notice.value = tr(`已生成 ${preview.value.script_name}`, `Generated ${preview.value.script_name}`)
  } catch (value) { setError(value) }
  finally { operationBusy.value = false }
}

async function deploy(): Promise<void> {
  operationBusy.value = true
  clearError()
  notice.value = ''
  try {
    const request = buildRequest()
    if (!preview.value) {
      preview.value = await offline.preview(request.payload)
    }
    const result = await offline.deploy(request.payload, request.firmwareFiles, request.flmFiles)
    deployedScriptName.value = result.script_name
    deployedModel.value = result.model
    notice.value = tr(`已部署 ${result.files.length} 个文件，脚本 ${result.script_name}`, `Deployed ${result.files.length} files with script ${result.script_name}`)
    await refreshDisk()
  } catch (value) { setError(value) }
  finally { operationBusy.value = false }
}

async function triggerOffline(): Promise<void> {
  operationBusy.value = true
  clearError()
  triggerLines.value = []
  try {
    const result = await offline.trigger(
      deployedModel.value as 'V2' | 'V3' | 'V4',
      deployedScriptName.value,
      (line) => {
        triggerLines.value.push(line)
        if (triggerLines.value.length > 200) triggerLines.value.shift()
      },
    )
    triggerLines.value = result.lines
    notice.value = result.status === 'completed' ? tr('脱机下载执行完成', 'Offline flashing completed') : tr('脱机下载执行失败', 'Offline flashing failed')
  } catch (value) { setError(value) }
  finally { operationBusy.value = false }
}

onMounted(async () => {
  await Promise.all([refreshDisk(), searchTargets()])
})
onActivated(() => {
  startSourcePolling()
  void startNativeDrops()
})
onDeactivated(() => {
  stopSourcePolling()
  stopNativeDrops()
})
onBeforeUnmount(() => {
  stopSourcePolling()
  stopNativeDrops()
})
</script>

<template>
  <div class="offline-page">
    <header class="status-strip">
      <div><span class="status-label">{{ tr('下载器', 'Probe') }}</span><b>{{ effectiveModel || tr('未选择', 'Not selected') }}</b></div>
      <div><span class="status-label">{{ tr('U 盘', 'USB Drive') }}</span><b :class="disk?.available ? 'ok' : 'bad'">{{ disk?.available ? disk.disk_path : tr('未发现', 'Not found') }}</b></div>
      <div><span class="status-label">{{ tr('脚本', 'Script') }}</span><b>{{ effectiveScriptName }}</b></div>
      <div class="status-actions">
        <button class="btn" :disabled="operationBusy" @click="refreshDisk">{{ tr('刷新 U 盘', 'Refresh USB Drive') }}</button>
      </div>
    </header>

    <div v-if="error" class="alert alert-error" data-testid="offline-error">
      <strong>{{ errorTitle || tr('脱机烧录无法继续', 'Offline flash could not continue') }}</strong>
      <span v-if="errorDetail" class="error-detail">{{ errorDetail }}</span>
      <details v-if="errorDetail" class="technical-error"><summary>{{ tr('技术详情', 'Technical details') }}</summary><code>{{ error }}</code></details>
    </div>
    <div v-if="selectionWarning" class="alert alert-warn" data-testid="offline-selection-warning">{{ selectionWarning }}</div>
    <div v-if="notice" class="alert alert-success">{{ notice }}</div>

    <div class="offline-workspace">
      <section class="work-panel target-panel">
        <div class="panel-heading"><h2>{{ tr('器件与下载算法', 'Targets and Flash Algorithms') }}</h2><label v-if="!hpmMode" class="btn btn-sm file-button">{{ tr('添加 FLM', 'Add FLM') }}<input type="file" accept=".flm" @change="addManualFlm"></label></div>
        <div class="target-search">
          <input v-model="targetQuery" class="form-input" data-testid="offline-target-search" @keydown.enter="searchTargets">
          <button class="btn" :disabled="targetBusy" @click="searchTargets">{{ tr('搜索器件', 'Search Targets') }}</button>
        </div>
        <div class="target-results">
          <button v-for="target in targets" :key="target.part_number" class="target-result" :disabled="targetBusy" @click="addTargetAlgorithms(target)">
            <span><b>{{ target.part_number }}</b><small>{{ target.vendor }}</small></span>
            <em>{{ targetAction(target) }}</em>
          </button>
        </div>
        <p v-if="hpmMode" class="hpm-mode">HPM ROM API · {{ tr('无需 Pack 或 FLM', 'No Pack or FLM required') }}</p>
        <div v-else class="algorithm-list">
          <div v-for="(item, index) in algorithms" :key="item.id" class="algorithm-row" :class="{ unavailable: !item.available && item.source_kind !== 'upload' }" data-testid="offline-algorithm-row">
            <div class="row-title"><input v-model="item.file_name" class="compact-input mono"><span>{{ item.origin === '本地文件' ? tr('本地文件', 'Local file') : item.origin }}</span><button class="icon-command" :title="tr('移除算法', 'Remove algorithm')" @click="removeAlgorithm(index)">×</button></div>
            <div v-if="!item.available && item.source_kind !== 'upload'" class="algorithm-warning" data-testid="offline-algorithm-unavailable">
              <span>{{ tr('U 盘中不可用', 'Unavailable on USB drive') }}</span>
              <button class="btn btn-sm" type="button" @click="chooseReplacement(item.id)">{{ tr('选择本地 FLM', 'Choose local FLM') }}</button>
            </div>
            <label>Flash<input v-model="item.flash_base" class="compact-input mono"></label>
            <label>RAM<input v-model="item.ram_base" class="compact-input mono"></label>
          </div>
          <p v-if="!algorithms.length" class="empty-state">{{ tr('尚未配置 FLM', 'No FLM configured') }}</p>
        </div>
      </section>

      <section class="work-panel firmware-panel" :class="{ dragging: firmwareDropActive }" data-testid="offline-firmware-drop-zone" @dragenter.prevent="firmwareDropActive = true" @dragover.prevent="firmwareDropActive = true" @dragleave.prevent="firmwareDropActive = false" @drop.prevent="dropFirmware">
        <div class="panel-heading"><h2>{{ tr('烧录顺序', 'Flash Sequence') }}</h2><button class="btn btn-sm" type="button" @click="browseFirmware">{{ tr('添加固件', 'Add Firmware') }}</button><input class="visually-hidden" data-testid="offline-firmware-input" type="file" multiple accept=".bin,.hex" @change="addFirmware"></div>
        <div class="firmware-list">
          <div v-for="(item, index) in firmwares" :key="item.id" class="firmware-row" data-testid="offline-firmware-row">
            <div class="sequence-number">{{ index + 1 }}</div>
            <div class="firmware-fields">
              <input v-model="item.file_name" class="compact-input mono file-name">
              <select v-if="!hpmMode" v-model="item.algorithm_id" class="compact-input">
                <option value="" disabled>{{ tr('选择 FLM', 'Select FLM') }}</option>
                <option v-for="algorithm in algorithms" :key="algorithm.id" :value="algorithm.id">{{ algorithm.file_name }}{{ !algorithm.available && algorithm.source_kind !== 'upload' ? ` · ${tr('不可用', 'unavailable')}` : '' }}</option>
              </select>
              <span v-else class="embedded-address">HPM ROM API</span>
              <input v-if="item.format === 'bin'" v-model="item.base_address" class="compact-input mono" :placeholder="tr('BIN 基地址', 'BIN base address')">
              <span v-else class="embedded-address">{{ tr('HEX 文件内地址', 'Address embedded in HEX') }}</span>
            </div>
            <div class="row-actions">
              <button class="icon-command" :title="tr('上移', 'Move up')" :disabled="index === 0" @click="moveFirmware(index, -1)">↑</button>
              <button class="icon-command" :title="tr('下移', 'Move down')" :disabled="index === firmwares.length - 1" @click="moveFirmware(index, 1)">↓</button>
              <button class="icon-command" :title="tr('移除固件', 'Remove firmware')" @click="firmwares.splice(index, 1)">×</button>
            </div>
          </div>
          <p v-if="!firmwares.length" class="empty-state">{{ tr('拖拽 BIN / HEX 到此工作区，或点击“添加固件”', 'Drop BIN / HEX into this workspace, or click Add Firmware') }}</p>
        </div>
      </section>

      <section class="work-panel settings-panel">
        <div class="panel-heading"><h2>{{ tr('量产配置', 'Production Settings') }}</h2></div>
        <label class="setting-row"><span>{{ tr('下载器型号', 'Probe Model') }}</span><select v-model="model" class="form-select" data-testid="offline-model" @change="modelChanged"><option value="" disabled>{{ tr('请选择', 'Select') }}</option><option value="V2">V2</option><option value="V3">V3</option><option value="V4">V4</option></select></label>
        <label v-if="hpmMode" class="setting-row"><span>{{ tr('HPM 板卡', 'HPM Board') }}</span><select v-model="hpmBoard" class="form-select"><option v-for="item in hpmBoards" :key="item" :value="item">{{ item }}</option></select></label>
        <label class="setting-row"><span>{{ tr('脚本文件名', 'Script File Name') }}</span><input v-model="scriptFieldName" class="form-input mono" data-testid="offline-script-name" :disabled="effectiveModel !== 'V4'"></label>
        <label class="setting-row"><span>{{ tr('自动烧录次数', 'Automatic Flash Count') }}</span><input v-model.number="automaticCount" type="number" min="1" max="9999" class="form-input" :disabled="effectiveModel === 'V2'"></label>
        <label class="setting-row"><span>{{ tr('IDCODE 超时', 'IDCODE Timeout') }}</span><input v-model.number="idcodeTimeout" type="number" min="500" max="600000" step="500" class="form-input"><em>ms</em></label>
        <label class="setting-row"><span>{{ tr('SWD 速率', 'SWD Rate') }}</span><select v-model.number="swdClock" class="form-select"><option :value="1000000">1 MHz</option><option :value="5000000">5 MHz</option><option :value="8000000">8 MHz</option><option :value="10000000">10 MHz</option></select></label>
        <div class="deploy-actions">
          <button class="btn" :disabled="operationBusy || !canBuild" @click="generatePreview">{{ tr('生成预览', 'Generate Preview') }}</button>
          <button class="btn btn-primary" data-testid="offline-deploy" :disabled="operationBusy || !canBuild" @click="deploy">{{ tr('部署到 U 盘', 'Deploy to USB Drive') }}</button>
          <button class="btn" data-testid="offline-trigger" :disabled="!canTrigger" @click="triggerOffline">{{ tr('触发测试', 'Run Test') }}</button>
        </div>
        <div class="script-preview">
          <div class="preview-title"><span>{{ preview?.script_name || effectiveScriptName }}</span><span>{{ preview?.model || effectiveModel }}</span></div>
          <pre>{{ preview?.script || tr('等待生成配置', 'Waiting to generate configuration') }}</pre>
        </div>
        <pre v-if="triggerLines.length" class="trigger-log">{{ triggerLines.join('\n') }}</pre>
      </section>
    </div>
    <input ref="replacementFlmInput" class="visually-hidden" type="file" accept=".flm" @change="replaceAlgorithm">
  </div>
</template>

<style scoped>
.firmware-panel{outline:2px solid transparent;outline-offset:-2px}.firmware-panel.dragging{outline-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--surface))}.visually-hidden{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.offline-page{min-height:0;display:flex;flex-direction:column;gap:10px}.status-strip{display:flex;align-items:center;gap:28px;min-height:46px;padding:8px 14px;border:1px solid var(--line);border-radius:var(--radius-card);background:var(--surface)}.status-strip>div{display:flex;align-items:baseline;gap:8px;min-width:0}.status-strip b{font-size:12px;font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.status-label{font-size:11px;color:var(--muted)}.status-actions{margin-left:auto}.ok{color:var(--success)}.bad{color:var(--danger)}.error-detail{display:block;margin-top:4px}.technical-error{margin-top:6px;color:var(--muted);font-size:11px}.technical-error code{display:block;margin-top:4px;white-space:pre-wrap;word-break:break-word;font:11px/1.45 var(--font-mono)}.offline-workspace{display:grid;grid-template-columns:minmax(260px,.9fr) minmax(360px,1.25fr) minmax(300px,1fr);gap:10px;min-height:620px}.work-panel{min-width:0;min-height:0;padding:14px;border:1px solid var(--line);border-radius:var(--radius-card);background:var(--surface);overflow:auto}.panel-heading{height:34px;display:flex;align-items:flex-start;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);margin-bottom:10px}.panel-heading h2{font-size:14px}.file-button{position:relative;overflow:hidden}.file-button input{position:absolute;inset:0;opacity:0;cursor:pointer}.target-search{display:grid;grid-template-columns:1fr auto;gap:6px}.target-results{display:grid;gap:5px;max-height:150px;overflow:auto;margin:8px 0 12px}.target-result{display:flex;align-items:center;justify-content:space-between;text-align:left;padding:7px 9px;border:1px solid var(--line);border-radius:5px;background:var(--surface-muted);color:var(--text);cursor:pointer}.target-result span{display:grid}.target-result small{font-size:10px;color:var(--muted)}.target-result em{font-style:normal;font-size:10px;color:var(--brand-text)}.hpm-mode{padding:12px;border:1px solid var(--line);border-radius:5px;background:var(--surface-muted);color:var(--success);font-size:12px}.algorithm-list,.firmware-list{display:grid;gap:7px}.algorithm-row,.firmware-row{border:1px solid var(--line);border-radius:5px;background:var(--surface-muted)}.algorithm-row{padding:8px}.algorithm-row.unavailable{border-color:var(--warn)}.algorithm-warning{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 8px;margin-bottom:5px;border-radius:4px;background:var(--warn-bg);color:var(--warn);font-size:11px}.row-title{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:7px;margin-bottom:7px}.row-title span{font-size:10px;color:var(--muted)}.algorithm-row>label{display:grid;grid-template-columns:42px 1fr;align-items:center;gap:6px;margin-top:5px;font-size:10px;color:var(--muted)}.compact-input{width:100%;height:27px;padding:0 7px;border:1px solid var(--control-border);border-radius:4px;background:var(--field-bg);color:var(--text);min-width:0}.mono{font-family:var(--font-mono)}.icon-command{width:27px;height:27px;border:1px solid var(--control-border);border-radius:4px;background:transparent;color:var(--muted);cursor:pointer}.icon-command:hover{color:var(--brand-text);border-color:var(--focus-ring)}.icon-command:disabled{opacity:.35;cursor:not-allowed}.firmware-row{display:grid;grid-template-columns:34px 1fr 28px;padding:8px;gap:7px}.sequence-number{display:grid;place-items:center;width:28px;height:28px;border-radius:4px;background:var(--surface-selected);font-family:var(--font-mono);font-weight:600}.firmware-fields{display:grid;grid-template-columns:minmax(120px,1.2fr) minmax(110px,1fr);gap:6px}.firmware-fields .file-name{grid-column:1/-1}.embedded-address{align-self:center;font-size:11px;color:var(--muted)}.row-actions{display:grid;gap:4px}.setting-row{display:grid;grid-template-columns:108px 1fr auto;align-items:center;gap:8px;margin-bottom:9px}.setting-row>span{font-size:12px;color:var(--muted);text-align:right}.setting-row em{font-size:10px;color:var(--muted);font-style:normal}.deploy-actions{display:flex;flex-wrap:wrap;gap:7px;margin:14px 0}.script-preview{border:1px solid var(--line);border-radius:5px;overflow:hidden}.preview-title{display:flex;justify-content:space-between;padding:6px 9px;background:var(--surface-muted);font-size:10px;color:var(--muted)}.script-preview pre,.trigger-log{margin:0;padding:10px;max-height:310px;overflow:auto;background:var(--terminal-bg);color:var(--terminal-text);font:11px/1.55 var(--font-mono);white-space:pre}.trigger-log{margin-top:8px;border-radius:5px}.empty-state{padding:20px 8px;text-align:center;color:var(--dim);font-size:12px}@media(max-width:1100px){.offline-workspace{grid-template-columns:1fr 1.25fr}.settings-panel{grid-column:1/-1}}@media(max-width:760px){.status-strip{align-items:flex-start;flex-wrap:wrap}.status-actions{margin-left:0}.offline-workspace{grid-template-columns:1fr}.settings-panel{grid-column:auto}.firmware-fields{grid-template-columns:1fr}.firmware-fields .file-name{grid-column:auto}}
</style>
