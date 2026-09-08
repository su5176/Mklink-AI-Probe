<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import ConfirmationDialog from '../components/ConfirmationDialog.vue'
import { provideConfirmation } from '../composables/useConfirmation'
import { useOfflineFlashApi } from '../composables/useOfflineFlashApi'
import { useOnlineFlashApi } from '../composables/useOnlineFlashApi'
import { tr } from '../composables/useLanguage'
import { listenForFirmwarePathDrops, pickFirmwareFiles, pickFlmFile } from '../lib/filePicker'
import type { TargetRecord } from '../types/onlineFlash'
import type {
  OfflineAlgorithmCandidate,
  OfflineAlgorithmConfig,
  OfflineConfigPayload,
  OfflineDiskStatus,
  OfflineFirmwareConfig,
  OfflinePreview,
  OfflineSecurityCapability,
} from '../types/offlineFlash'

interface AlgorithmRow extends Omit<OfflineAlgorithmCandidate, 'source_kind'> {
  source_kind: 'upload' | 'pack' | 'profile' | 'existing'
  file: File | null
  source_path: string | null
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
const { message: confirmationMessage, confirm: confirmRisk, answer: answerConfirmation } = provideConfirmation()

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
const securityCapability = ref<OfflineSecurityCapability | null>(null)
const securityLoading = ref(false)
const eraseAllBeforeDownload = ref(false)
const unlockBeforeDownload = ref(false)
const lockAfterDownload = ref(false)
const securityVoltageMv = ref(3300)
const targets = ref<TargetRecord[]>([])
const targetBusy = ref(false)
const targetSearchBox = ref<HTMLElement | null>(null)
const targetSuggestionsOpen = ref(false)
const activeTargetSuggestion = ref(-1)
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
let securityRequestSequence = 0
let targetSearchTimer: ReturnType<typeof setTimeout> | null = null

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
  item.source_kind === 'upload' ? !(item.file || item.source_path) : !item.available
)))
const selectionWarning = computed(() => unavailableSelectedAlgorithms.value.length
  ? tr(
    `烧录顺序使用的下载算法不可用：${unavailableSelectedAlgorithms.value.map(item => item.file_name).join('、')}。请选择本地 FLM 替换，或为固件选择其他算法。`,
    `The flash sequence uses unavailable algorithms: ${unavailableSelectedAlgorithms.value.map(item => item.file_name).join(', ')}. Choose a local FLM replacement or select another algorithm.`,
  )
  : '')
const securityRequested = computed(() => unlockBeforeDownload.value || lockAfterDownload.value)
const securityReady = computed(() => (
  !securityRequested.value
  || (
    !!securityCapability.value?.supported
    && securityCapability.value.voltage_options_mv.includes(securityVoltageMv.value)
  )
))
const canBuild = computed(() => (
  !!effectiveModel.value
  && !!disk.value?.available
  && securityReady.value
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
  [model, scriptName, automaticCount, idcodeTimeout, swdClock, targetPart, hpmBoard, algorithms, firmwares, eraseAllBeforeDownload, unlockBeforeDownload, lockAfterDownload, securityVoltageMv],
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

watch([model, targetPart], () => { void refreshSecurityCapability() })

function message(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}

function setError(value: unknown): void {
  const raw = message(value)
  error.value = raw
  errorTitle.value = ''
  errorDetail.value = raw
  if (/offline file operation failed/i.test(raw)) {
    errorTitle.value = tr('U 盘文件更新失败', 'USB file update failed')
    errorDetail.value = tr('内容相同的文件会直接复用。请查看技术详情中的文件名；若需要替换，请关闭占用它的程序，并确认 U 盘可写后重试。', 'Identical files are reused. Check the file name in Technical details; if replacement is needed, close programs using it and confirm the USB drive is writable before retrying.')
    return
  }
  if (/Unable to connect to MKLink CDC port|could not open port|PermissionError|Access is denied|拒绝访问/i.test(raw)) {
    errorTitle.value = tr('无法连接下载器命令串口', 'Cannot connect to probe command port')
    errorDetail.value = tr('请先断开其他 WebGUI、桌面客户端或串口工具中的探针连接，确认 USB 连接正常，再点击触发测试。', 'Disconnect the probe in other WebGUI, desktop or serial tools, check the USB connection, then run the test again.')
    return
  }
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
  if (/local FLM source|missing uploaded FLM source|FLM source does not exist/i.test(raw)) {
    errorTitle.value = tr('本地 FLM 文件不可用', 'Local FLM file unavailable')
    errorDetail.value = tr('请重新选择该 FLM。若文件位于下载器 U 盘中也可以直接选择，部署时会先安全暂存再更新，不会原地覆盖。', 'Choose the FLM again. Files already on the probe USB drive are supported; deployment stages them safely before updating instead of overwriting in place.')
    return
  }
  if (/failed to fetch|networkerror|load failed/i.test(raw)) {
    errorTitle.value = tr('本地后端连接已中断', 'Local backend connection interrupted')
    errorDetail.value = tr('请确认右上角显示“后端正常”，然后刷新页面或重新打开 WebGUI。', 'Confirm the header shows Backend online, then refresh the page or reopen WebGUI.')
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

async function refreshSecurityCapability(): Promise<void> {
  const requestSequence = ++securityRequestSequence
  answerConfirmation(false)
  unlockBeforeDownload.value = false
  lockAfterDownload.value = false
  securityCapability.value = null
  securityLoading.value = false
  if (!model.value || !targetPart.value || hpmMode.value) return
  securityLoading.value = true
  try {
    const capability = await offline.getSecurityStatus(model.value, targetPart.value)
    if (requestSequence !== securityRequestSequence) return
    securityCapability.value = capability
    securityVoltageMv.value = capability.default_voltage_mv ?? 3300
  } catch (value) {
    if (requestSequence === securityRequestSequence) setError(value)
  } finally {
    if (requestSequence === securityRequestSequence) securityLoading.value = false
  }
}

async function toggleUnlock(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const requested = input.checked
  input.checked = unlockBeforeDownload.value
  if (!requested) {
    unlockBeforeDownload.value = false
    return
  }
  if (!securityCapability.value?.unlock_supported) return
  const voltage = `${(securityVoltageMv.value / 1000).toFixed(securityVoltageMv.value === 5000 ? 0 : 1)}V`
  const voltageWarning = securityVoltageMv.value === 5000
    ? tr('5V 可能损坏不耐受的目标板，必须确认供电路径和全部负载耐压。', '5V can damage an incompatible target; confirm the power path and voltage rating of every load. ')
    : ''
  if (await confirmRisk(tr(
    `解锁会通过下载器 VCC 以 ${voltage} 断电复位，并永久删除芯片全部 Flash 数据。${voltageWarning}该操作不可撤销，确定启用“下载前解锁”？`,
    `Unlocking power-cycles probe VCC at ${voltage} and permanently deletes all chip Flash data. ${voltageWarning}This cannot be undone. Enable Unlock before download?`,
  ))) unlockBeforeDownload.value = true
}

async function changeSecurityVoltage(event: Event): Promise<void> {
  const select = event.target as HTMLSelectElement
  const requested = Number(select.value)
  const previous = securityVoltageMv.value
  select.value = String(previous)
  if (!securityCapability.value?.voltage_options_mv.includes(requested)) return
  if (!securityRequested.value) {
    securityVoltageMv.value = requested
    return
  }
  const voltage = `${(requested / 1000).toFixed(requested === 5000 ? 0 : 1)}V`
  const warning = requested === 5000
    ? tr('5V 可能损坏不耐受的目标板，请确认目标板供电路径和全部负载均可承受 5V。', '5V can damage an incompatible target. Confirm that the target power path and every load tolerate 5V.')
    : tr(`请确认目标板需要由下载器恢复为 ${voltage} 供电。`, `Confirm that the target must be restored to ${voltage} from the probe.`)
  if (await confirmRisk(tr(
    `安全操作已启用。${warning}确定把断电恢复电压改为 ${voltage}？`,
    `A security operation is enabled. ${warning} Change the power-restore voltage to ${voltage}?`,
  ))) securityVoltageMv.value = requested
}

async function toggleEraseAll(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const requested = input.checked
  input.checked = eraseAllBeforeDownload.value
  if (!requested) {
    eraseAllBeforeDownload.value = false
    return
  }
  if (hpmMode.value || !firmwares.value.length) return
  if (await confirmRisk(tr(
    `全片擦除会在下载前永久删除烧录顺序第一个固件所对应 Flash 的全部数据，包括未被所选固件覆盖的引导程序、参数和用户数据。确定启用“下载前全片擦除”？`,
    `Chip erase permanently deletes all data from the Flash selected by the first firmware before programming, including bootloaders, parameters, and user data not covered by the selected images. Enable Chip erase before download?`,
  ))) eraseAllBeforeDownload.value = true
}

async function toggleLock(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const requested = input.checked
  input.checked = lockAfterDownload.value
  if (!requested) {
    lockAfterDownload.value = false
    return
  }
  if (!securityCapability.value?.lock_supported) return
  const voltage = `${(securityVoltageMv.value / 1000).toFixed(securityVoltageMv.value === 5000 ? 0 : 1)}V`
  const voltageWarning = securityVoltageMv.value === 5000
    ? tr('5V 可能损坏不耐受的目标板，必须确认供电路径和全部负载耐压。', '5V can damage an incompatible target; confirm the power path and voltage rating of every load. ')
    : ''
  if (await confirmRisk(tr(
    `加锁会在固件下载成功后写入读保护，并通过下载器 VCC 以 ${voltage} 断电复位。${voltageWarning}之后读取、RTT 和调试会受限，再次解锁将永久删除全部 Flash 数据。确定启用“下载后加锁”？`,
    `Locking enables read protection after a successful download and power-cycles probe VCC at ${voltage}. ${voltageWarning}Reading, RTT, and debugging will then be restricted; a later unlock permanently erases all Flash. Enable Lock after download?`,
  ))) lockAfterDownload.value = true
}

async function searchTargets(openSuggestions = true): Promise<void> {
  const query = targetQuery.value.trim()
  if (!query) {
    targets.value = []
    targetSuggestionsOpen.value = false
    activeTargetSuggestion.value = -1
    return
  }
  targetBusy.value = true
  clearError()
  try {
    targets.value = await online.searchTargets(query, { limit: 30 })
    activeTargetSuggestion.value = targets.value.length ? 0 : -1
    targetSuggestionsOpen.value = openSuggestions && targets.value.length > 0
  }
  catch (value) { setError(value) }
  finally { targetBusy.value = false }
}

function scheduleTargetSearch(): void {
  if (targetSearchTimer !== null) clearTimeout(targetSearchTimer)
  activeTargetSuggestion.value = -1
  targetSearchTimer = setTimeout(() => { void searchTargets(true) }, 150)
}

function targetSearchFocus(): void {
  targetSuggestionsOpen.value = targets.value.length > 0
}

function targetSearchFocusOut(): void {
  setTimeout(() => {
    if (!targetSearchBox.value?.contains(document.activeElement)) targetSuggestionsOpen.value = false
  }, 0)
}

function targetSearchKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    targetSuggestionsOpen.value = false
    activeTargetSuggestion.value = -1
    return
  }
  if (!targets.value.length) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    targetSuggestionsOpen.value = true
    const delta = event.key === 'ArrowDown' ? 1 : -1
    const start = activeTargetSuggestion.value < 0 ? (delta > 0 ? -1 : 0) : activeTargetSuggestion.value
    activeTargetSuggestion.value = (start + delta + targets.value.length) % targets.value.length
    return
  }
  if (event.key === 'Enter' && targetSuggestionsOpen.value && activeTargetSuggestion.value >= 0) {
    event.preventDefault()
    void addTargetAlgorithms(targets.value[activeTargetSuggestion.value])
  }
}

function mergeAlgorithms(items: OfflineAlgorithmCandidate[]): void {
  for (const item of items) {
    const duplicate = algorithms.value.some(existing => (
      existing.file_name.toLowerCase() === item.file_name.toLowerCase()
      && existing.flash_base === item.flash_base
      && existing.ram_base === item.ram_base
      && (existing.source_token || existing.id) === (item.source_token || item.id)
    ))
    if (!duplicate) algorithms.value.push({ ...item, file: null, source_path: null })
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
    targetQuery.value = target.part_number
    targetSuggestionsOpen.value = false
    activeTargetSuggestion.value = -1
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

function addManualFlmSource(source: string | File): void {
  const file = source instanceof File ? source : null
  const sourcePath = typeof source === 'string' ? source : null
  const name = file?.name || sourcePath?.split(/[\\/]/).pop() || ''
  if (!name.toLowerCase().endsWith('.flm')) {
    setError(new Error(tr('下载算法必须是 .FLM 文件', 'Flash algorithm must be an .FLM file')))
    return
  }
  const id = nextId('flm')
  const diskPath = disk.value?.disk_path?.replace(/[\\/]+$/, '').toLowerCase()
  const onProbe = !!(sourcePath && diskPath && sourcePath.toLowerCase().startsWith(`${diskPath}\\`))
  algorithms.value.push({
    id,
    file_name: name,
    flash_base: '0x08000000',
    ram_base: '0x20000000',
    source_kind: 'upload',
    source_token: null,
    origin: onProbe ? tr('下载器 U 盘文件', 'Probe USB file') : tr('本地文件', 'Local file'),
    available: true,
    on_probe: onProbe,
    file,
    source_path: sourcePath,
  })
  if (algorithms.value.length === 1) {
    firmwares.value.forEach(item => { item.algorithm_id = id })
  }
}

async function browseManualFlm(): Promise<void> {
  const source = await pickFlmFile()
  if (source) addManualFlmSource(source)
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
  algorithm.source_path = null
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
      const stamp = `${status.size}:${status.mtime_ns}:${status.sha256 ?? ''}`
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
      if (item.file) uploadIndex = flmFiles.push(item.file) - 1
      else if (!item.source_path) throw new Error(tr(`请选择 ${item.file_name} 的 FLM 文件`, `Select the FLM file for ${item.file_name}`))
    }
    return {
      id: item.id,
      file_name: item.file_name,
      flash_base: item.flash_base,
      ram_base: item.ram_base,
      source_kind: item.source_kind,
      source_token: item.source_token,
      upload_index: uploadIndex,
      source_path: item.source_path,
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
      erase_all_before_download: eraseAllBeforeDownload.value,
      unlock_before_download: unlockBeforeDownload.value,
      lock_after_download: lockAfterDownload.value,
      security_voltage_mv: securityRequested.value ? securityVoltageMv.value : null,
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
  await Promise.all([refreshDisk(), searchTargets(false)])
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
  if (targetSearchTimer !== null) clearTimeout(targetSearchTimer)
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
        <div class="panel-heading step-heading">
          <div class="step-title"><span>01</span><div><h2>{{ tr('器件与下载算法', 'Targets and Flash Algorithms') }}</h2><small>{{ tr('输入型号并从联想结果中选定器件', 'Type a model and select a suggested target') }}</small></div></div>
          <button v-if="!hpmMode" class="btn btn-sm" type="button" data-testid="offline-add-flm" @click="browseManualFlm">{{ tr('添加本地 FLM', 'Add Local FLM') }}</button>
        </div>
        <div ref="targetSearchBox" class="target-combobox" @focusout="targetSearchFocusOut">
          <div class="target-search">
            <input v-model="targetQuery" class="form-input" data-testid="offline-target-search" type="search" role="combobox" aria-autocomplete="list" aria-controls="offline-target-suggestions" :aria-expanded="targetSuggestionsOpen" :aria-activedescendant="targetSuggestionsOpen && activeTargetSuggestion >= 0 ? `offline-target-option-${activeTargetSuggestion}` : undefined" :placeholder="tr('搜索型号 / 厂商 / 系列', 'Search model / vendor / family / series')" @input="scheduleTargetSearch" @focus="targetSearchFocus" @keydown="targetSearchKeydown">
            <button class="btn" :disabled="targetBusy" @click="searchTargets(true)">{{ targetBusy ? tr('搜索中…', 'Searching…') : tr('搜索器件', 'Search Targets') }}</button>
          </div>
          <div v-show="targetSuggestionsOpen && targets.length" id="offline-target-suggestions" class="target-results" role="listbox">
            <button v-for="(target, index) in targets" :id="`offline-target-option-${index}`" :key="target.part_number" class="target-result" :class="{ active: index === activeTargetSuggestion || targetPart === target.part_number }" type="button" role="option" :aria-selected="targetPart === target.part_number" :disabled="targetBusy" @mouseenter="activeTargetSuggestion = index" @click="addTargetAlgorithms(target)">
              <span><b>{{ target.part_number }}</b><small>{{ target.vendor }} · {{ target.pack_id || tr('内置', 'Built-in') }}</small></span>
              <em>{{ targetAction(target) }}</em>
            </button>
          </div>
        </div>
        <p v-if="targetPart" class="selected-target"><span>{{ tr('已选器件', 'Selected target') }}</span><b>{{ targetPart }}</b></p>
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
        <div class="panel-heading step-heading"><div class="step-title"><span>02</span><div><h2>{{ tr('烧录顺序', 'Flash Sequence') }}</h2><small>{{ tr('固件按从上到下顺序依次写入', 'Firmware is programmed from top to bottom') }}</small></div></div><button class="btn btn-sm" type="button" @click="browseFirmware">{{ tr('添加固件', 'Add Firmware') }}</button><input class="visually-hidden" data-testid="offline-firmware-input" type="file" multiple accept=".bin,.hex" @change="addFirmware"></div>
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
        <div class="panel-heading step-heading"><div class="step-title"><span>03</span><div><h2>{{ tr('量产配置与部署', 'Production Settings & Deploy') }}</h2><small>{{ tr('确认安全选项，部署后再触发一次真机验证', 'Confirm security options, deploy, then run one hardware test') }}</small></div></div></div>
        <label class="setting-row"><span>{{ tr('下载器型号', 'Probe Model') }}</span><select v-model="model" class="form-select" data-testid="offline-model" @change="modelChanged"><option value="" disabled>{{ tr('请选择', 'Select') }}</option><option value="V2">V2</option><option value="V3">V3</option><option value="V4">V4</option></select></label>
        <label v-if="hpmMode" class="setting-row"><span>{{ tr('HPM 板卡', 'HPM Board') }}</span><select v-model="hpmBoard" class="form-select"><option v-for="item in hpmBoards" :key="item" :value="item">{{ item }}</option></select></label>
        <label class="setting-row"><span>{{ tr('脚本文件名', 'Script File Name') }}</span><input v-model="scriptFieldName" class="form-input mono" data-testid="offline-script-name" :disabled="effectiveModel !== 'V4'"></label>
        <label class="setting-row"><span>{{ tr('自动烧录次数', 'Automatic Flash Count') }}</span><input v-model.number="automaticCount" type="number" min="1" max="9999" class="form-input" :disabled="effectiveModel === 'V2'"></label>
        <label class="setting-row"><span>{{ tr('IDCODE 超时', 'IDCODE Timeout') }}</span><input v-model.number="idcodeTimeout" type="number" min="500" max="600000" step="500" class="form-input"><em>ms</em></label>
        <label class="setting-row"><span>{{ tr('SWD 速率', 'SWD Rate') }}</span><select v-model.number="swdClock" class="form-select"><option :value="1000000">1 MHz</option><option :value="5000000">5 MHz</option><option :value="8000000">8 MHz</option><option :value="10000000">10 MHz</option></select></label>
        <div class="security-settings">
          <div class="security-title">
            <span>{{ tr('擦除与安全操作', 'Erase and Security Operations') }}</span>
            <em v-if="securityLoading">{{ tr('正在检查器件支持…', 'Checking target support…') }}</em>
            <em v-else-if="securityCapability" :class="securityCapability.supported ? 'ok' : 'bad'">{{ securityCapability.supported ? tr('加锁/解锁已验证', 'Lock/unlock validated') : tr('加锁/解锁未支持', 'Lock/unlock unsupported') }}</em>
          </div>
          <label class="security-option">
            <input data-testid="offline-erase-all" type="checkbox" aria-describedby="offline-erase-all-hint" :checked="eraseAllBeforeDownload" :disabled="hpmMode || !firmwares.length" @change="toggleEraseAll">
            <span>{{ tr('下载前全片擦除（第一个固件对应的 Flash）', 'Chip erase before download (Flash selected by first firmware)') }}</span>
          </label>
          <p v-if="!hpmMode" id="offline-erase-all-hint" class="security-reason" :class="{ 'erase-all-warning': eraseAllBeforeDownload }" data-testid="offline-erase-all-hint">{{ tr('默认按扇区擦除，通常无需勾选全片擦除。全片擦除会显著增加烧录时间。', 'Sector erase is used by default; chip erase is usually unnecessary. Chip erase significantly increases programming time.') }}</p>
          <label class="security-option">
            <input data-testid="offline-unlock" type="checkbox" :checked="unlockBeforeDownload" :disabled="!securityCapability?.unlock_supported" @change="toggleUnlock">
            <span>{{ tr('下载前解锁（永久擦除全部 Flash）', 'Unlock before download (permanently erases all Flash)') }}</span>
          </label>
          <label class="security-option">
            <input data-testid="offline-lock" type="checkbox" :checked="lockAfterDownload" :disabled="!securityCapability?.lock_supported" @change="toggleLock">
            <span>{{ tr('下载成功后加锁', 'Lock after successful download') }}</span>
          </label>
          <label v-if="securityCapability?.supported" class="setting-row security-voltage"><span>{{ tr('断电恢复电压', 'Power Restore Voltage') }}</span><select :value="securityVoltageMv" class="form-select" data-testid="offline-security-voltage" @change="changeSecurityVoltage"><option v-for="voltage in securityCapability.voltage_options_mv" :key="voltage" :value="voltage">{{ (voltage / 1000).toFixed(voltage === 5000 ? 0 : 1) }} V</option></select></label>
          <p v-if="securityLoading" class="security-reason">{{ tr('正在按下载器型号和已选器件加载安全操作白名单。', 'Loading the security-operation whitelist for the probe and selected target.') }}</p>
          <p v-else-if="securityCapability && !securityCapability.supported" class="security-reason">{{ securityCapability.reason }}</p>
          <p v-else-if="securityCapability?.supported" class="security-reason">{{ tr('加锁与解锁只对已真机验证的器件开放；配置、器件 ID、容量和 FLM 均会严格校验。', 'Lock and unlock are enabled only for hardware-validated targets; configuration, device ID, density, and FLM are strictly verified.') }}</p>
        </div>
        <div class="deploy-actions">
          <button class="btn" :disabled="operationBusy || !canBuild" @click="generatePreview">{{ tr('生成预览', 'Generate Preview') }}</button>
          <button class="btn btn-primary" data-testid="offline-deploy" :disabled="operationBusy || !canBuild" @click="deploy">{{ tr('部署到 U 盘', 'Deploy to USB Drive') }}</button>
          <button class="btn" data-testid="offline-trigger" :disabled="!canTrigger" @click="triggerOffline">{{ tr('触发测试', 'Run Test') }}</button>
        </div>
        <p class="action-guide">{{ tr('建议流程：先生成预览检查脚本 → 部署到 U 盘 → 点击“触发测试”验证目标板。', 'Recommended: inspect the generated script → deploy to USB → run a hardware test.') }}</p>
        <div class="script-preview">
          <div class="preview-title"><span>{{ preview?.script_name || effectiveScriptName }}</span><span>{{ preview?.model || effectiveModel }}</span></div>
          <pre>{{ preview?.script || tr('等待生成配置', 'Waiting to generate configuration') }}</pre>
        </div>
        <pre v-if="triggerLines.length" class="trigger-log">{{ triggerLines.join('\n') }}</pre>
      </section>
    </div>
    <input ref="replacementFlmInput" class="visually-hidden" type="file" accept=".flm" @change="replaceAlgorithm">
    <ConfirmationDialog v-if="confirmationMessage !== null" :message="confirmationMessage" @answer="answerConfirmation" />
  </div>
</template>

<style scoped>
.firmware-panel{outline:2px solid transparent;outline-offset:-2px}.firmware-panel.dragging{outline-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--surface))}.visually-hidden{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.offline-page{min-height:0;display:flex;flex-direction:column;gap:10px}.status-strip{display:flex;align-items:center;gap:28px;min-height:46px;padding:8px 14px;border:1px solid var(--line);border-radius:var(--radius-card);background:var(--surface)}.status-strip>div{display:flex;align-items:baseline;gap:8px;min-width:0}.status-strip b{font-size:12px;font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.status-label{font-size:11px;color:var(--muted)}.status-actions{margin-left:auto}.ok{color:var(--success)}.bad{color:var(--danger)}.error-detail{display:block;margin-top:4px}.technical-error{margin-top:6px;color:var(--muted);font-size:11px}.technical-error code{display:block;margin-top:4px;white-space:pre-wrap;word-break:break-word;font:11px/1.45 var(--font-mono)}.offline-workspace{display:grid;grid-template-columns:minmax(260px,.9fr) minmax(360px,1.25fr) minmax(300px,1fr);gap:10px;min-height:620px}.work-panel{min-width:0;min-height:0;padding:14px;border:1px solid var(--line);border-radius:var(--radius-card);background:var(--surface);overflow:auto}.panel-heading{height:34px;display:flex;align-items:flex-start;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);margin-bottom:10px}.panel-heading h2{font-size:14px}.file-button{position:relative;overflow:hidden}.file-button input{position:absolute;inset:0;opacity:0;cursor:pointer}.target-search{display:grid;grid-template-columns:1fr auto;gap:6px}.target-results{display:grid;gap:5px;max-height:150px;overflow:auto;margin:8px 0 12px}.target-result{display:flex;align-items:center;justify-content:space-between;text-align:left;padding:7px 9px;border:1px solid var(--line);border-radius:5px;background:var(--surface-muted);color:var(--text);cursor:pointer}.target-result span{display:grid}.target-result small{font-size:10px;color:var(--muted)}.target-result em{font-style:normal;font-size:10px;color:var(--brand-text)}.hpm-mode{padding:12px;border:1px solid var(--line);border-radius:5px;background:var(--surface-muted);color:var(--success);font-size:12px}.algorithm-list,.firmware-list{display:grid;gap:7px}.algorithm-row,.firmware-row{border:1px solid var(--line);border-radius:5px;background:var(--surface-muted)}.algorithm-row{padding:8px}.algorithm-row.unavailable{border-color:var(--warn)}.algorithm-warning{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 8px;margin-bottom:5px;border-radius:4px;background:var(--warn-bg);color:var(--warn);font-size:11px}.row-title{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:7px;margin-bottom:7px}.row-title span{font-size:10px;color:var(--muted)}.algorithm-row>label{display:grid;grid-template-columns:42px 1fr;align-items:center;gap:6px;margin-top:5px;font-size:10px;color:var(--muted)}.compact-input{width:100%;height:27px;padding:0 7px;border:1px solid var(--control-border);border-radius:4px;background:var(--field-bg);color:var(--text);min-width:0}.mono{font-family:var(--font-mono)}.icon-command{width:27px;height:27px;border:1px solid var(--control-border);border-radius:4px;background:transparent;color:var(--muted);cursor:pointer}.icon-command:hover{color:var(--brand-text);border-color:var(--focus-ring)}.icon-command:disabled{opacity:.35;cursor:not-allowed}.firmware-row{display:grid;grid-template-columns:34px 1fr 28px;padding:8px;gap:7px}.sequence-number{display:grid;place-items:center;width:28px;height:28px;border-radius:4px;background:var(--surface-selected);font-family:var(--font-mono);font-weight:600}.firmware-fields{display:grid;grid-template-columns:minmax(120px,1.2fr) minmax(110px,1fr);gap:6px}.firmware-fields .file-name{grid-column:1/-1}.embedded-address{align-self:center;font-size:11px;color:var(--muted)}.row-actions{display:grid;gap:4px}.setting-row{display:grid;grid-template-columns:108px 1fr auto;align-items:center;gap:8px;margin-bottom:9px}.setting-row>span{font-size:12px;color:var(--muted);text-align:right}.setting-row em{font-size:10px;color:var(--muted);font-style:normal}.deploy-actions{display:flex;flex-wrap:wrap;gap:7px;margin:14px 0}.script-preview{border:1px solid var(--line);border-radius:5px;overflow:hidden}.preview-title{display:flex;justify-content:space-between;padding:6px 9px;background:var(--surface-muted);font-size:10px;color:var(--muted)}.script-preview pre,.trigger-log{margin:0;padding:10px;max-height:310px;overflow:auto;background:var(--terminal-bg);color:var(--terminal-text);font:11px/1.55 var(--font-mono);white-space:pre}.trigger-log{margin-top:8px;border-radius:5px}.empty-state{padding:20px 8px;text-align:center;color:var(--dim);font-size:12px}@media(max-width:1100px){.offline-workspace{grid-template-columns:1fr 1.25fr}.settings-panel{grid-column:1/-1}}@media(max-width:760px){.status-strip{align-items:flex-start;flex-wrap:wrap}.status-actions{margin-left:0}.offline-workspace{grid-template-columns:1fr}.settings-panel{grid-column:auto}.firmware-fields{grid-template-columns:1fr}.firmware-fields .file-name{grid-column:auto}}
.security-settings{margin:12px 0;padding:10px;border:1px solid var(--border);border-radius:5px;background:var(--bg)}
.security-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;font-size:12px;font-weight:600}
.security-title em{font-size:10px;font-style:normal}
.security-option{display:flex;align-items:flex-start;gap:7px;margin:7px 0;font-size:11px;line-height:1.45}
.security-option input{margin-top:2px}
.security-voltage{margin:9px 0 4px}
.security-reason{margin:7px 0 0;color:var(--muted);font-size:10px;line-height:1.5}
.step-heading{height:auto;min-height:44px;align-items:flex-start}
.step-title{display:flex;gap:8px;min-width:0}
.step-title>span{display:grid;place-items:center;flex:0 0 27px;height:27px;border-radius:5px;background:color-mix(in srgb,var(--accent) 13%,var(--bg));color:var(--accent);font:600 11px var(--font-mono)}
.step-title h2{margin:0}
.step-title small{display:block;margin-top:2px;color:var(--muted);font-size:9px;font-weight:400}
.target-combobox{position:relative}
.target-result.active{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,#fff)}
.selected-target{display:flex;align-items:center;justify-content:space-between;margin:8px 0 10px;padding:7px 9px;border-radius:5px;background:var(--bg);font-size:10px;color:var(--muted)}
.selected-target b{color:var(--success);font-family:var(--font-mono)}
.action-guide{margin:-7px 0 12px;color:var(--muted);font-size:10px;line-height:1.5}
.security-reason.erase-all-warning{padding:8px;border-left:3px solid var(--warn);background:color-mix(in srgb,var(--warn) 10%,var(--bg));color:var(--warn);font-size:11px}
</style>
