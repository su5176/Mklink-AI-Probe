export interface PortInfo {
  device: string
  description: string
  manufacturer: string
  vid: number | null
  pid: number | null
}

export interface McuProfile {
  key: string
  name: string
  [k: string]: unknown
}

export interface ProjectConfig {
  com_port?: string
  mcu_key?: string
  swd_clock?: string
}

export interface ProjectInfo {
  hex_path?: string
  map_path?: string
  flm_path?: string
  flm_name?: string
  flash_base?: string
  axf_path?: string
}

export interface RttConfig {
  rtt_addr?: string
  /** RTT 控制块存储方式：0 = 动态搜寻（默认），1 = 静态编译（C 宏固定地址） */
  rtt_storage_mode?: 0 | 1
  search_size?: number
  channel?: number
  autostart?: boolean
  integrated?: boolean
  [k: string]: unknown
}

export interface ConfigStatus {
  is_valid: boolean
  has_config: boolean
  has_project: boolean
  has_rtt_config: boolean
  errors: string[]
  warnings: string[]
  flm_on_microkeen: boolean
}

export interface AxlStatus {
  loaded: boolean
  axf_path?: string | null
  elf_backend?: 'builtin' | 'external'
  elf_available?: boolean
  builtin_elf_available?: boolean
  builtin_elf_version?: string | null
  external_elf_available?: boolean
  external_source_lookup_available?: boolean
  readelf_available?: boolean
  addr2line_available?: boolean
  variable_count?: number
  struct_count?: number
  enum_count?: number
  error?: string
}

export interface DeviceStatus {
  connected: boolean
  state: string
  mcu: string | null
  idcode: string | null
  port: string | null
  axf: AxlStatus
}

export interface MicrokeenInfo {
  disk_path: string | null
  flm_dir: string | null
  available: boolean
}

export interface ConnectRequest {
  port?: string
  axf?: string
  mcu?: string
  elf_backend?: 'builtin' | 'external'
  restore_last?: boolean
}

export interface FlashRequest {
  firmware: string
  verify?: boolean
  reset_after?: boolean
}

export interface JsonRpcRequest {
  jsonrpc: '2.0'
  method: string
  params?: Record<string, unknown>
  id?: number | string
  token?: string
}

export interface JsonRpcResponse {
  jsonrpc: '2.0'
  result?: unknown
  error?: { code: number; message: string }
  id: number | string | null
}

export type DashboardType = 'rtt' | 'serial' | 'modbus' | 'superwatch' | 'systemview'

// SystemView（RTOS 跟踪）解码事件——由后端 SystemViewParser 产出
export interface SystemViewEvent {
  _t?: number
  event?: string
  kind: string
  task_id?: number
  task_name?: string
  isr_id?: number
  isr_name?: string
  cause?: number
  prio?: number
  t_ticks?: number
  t_us?: number
  delta_ticks?: number
  cpu_delta_us?: number
}

export interface DashboardInfo {
  running: boolean
  url: string | null
}

export interface DashboardStatus {
  [key: string]: DashboardInfo
}

// SSE data point
export interface DataPoint {
  _t?: number
  _event?: string
  [channel: string]: number | string | object | undefined
}

// HardFault detail
export interface HardFaultCallFrame {
  index: number
  address: number
  lookup_address: number
  function?: string | null
  location?: string
  source: 'exception_pc' | 'exception_lr' | 'stack_scan'
  confidence: 'exact' | 'high' | 'heuristic'
  stack_address?: number
}

export interface HardFaultExceptionStack {
  pointer: 'msp' | 'psp'
  pointer_address: number
  frame_address: number
  frame_offset: number
  exc_return?: number | null
  handler_lr: number
  extended_frame: boolean
}

export interface HardFaultDetail {
  fault: boolean | null
  cfsr?: number
  hfsr?: number
  cfsr_flags?: string[]
  hfsr_flags?: string[]
  stack_frame?: Record<string, number> | null
  source_locations?: Record<string, string> | null
  summary?: string
  fault_function?: string | null
  fault_location?: string | null
  exception_stack?: HardFaultExceptionStack | null
  call_stack?: HardFaultCallFrame[]
  core_registers?: Record<string, number> | null
}

// Core registers
export interface CoreRegisters {
  [name: string]: number
}

// Symbol search result
export interface SymbolEntry {
  name: string
  address: number | string | null
  type: string
  size: number
}

// Symbol type info
export interface SymbolTypeInfo {
  name: string
  found: boolean
  type?: string
  size?: number
  address?: number | string | null
  members?: unknown[]
}

export interface RttFindResponse {
  found: boolean
  addr?: string
  source?: string
  source_path?: string
  details?: string[]
  warnings?: string[]
}

export interface RttWriteResponse {
  sent_bytes: number
}

export type SymbolScalarKind = 'signed' | 'unsigned' | 'float' | 'bool' | 'enum'

export interface SymbolDescriptor {
  path: string
  address: number
  type_name: string
  scalar_kind: SymbolScalarKind
  size: number
  writable: boolean
  enum_values: Record<string, number>
  enum_signed?: boolean
  parent_path: string | null
  overlapping?: boolean
  source?: 'dwarf' | 'c_override'
}

export interface SymbolContainerDescriptor {
  path: string
  address: number
  type_name: string
  size: number
  reason: 'unsupported_layout'
}

export type SymbolBrowseKind = 'leaf' | 'branch' | 'range' | 'container'

export interface SymbolBrowseNode {
  key: string
  path: string
  label: string
  kind: SymbolBrowseKind
  type_name: string
  size: number
  address: number | null
  descriptor: SymbolDescriptor | null
  container: SymbolContainerDescriptor | null
  child_count: number | null
  range_start: number | null
  range_end: number | null
  array_dimensions?: readonly number[]
  snapshot_eligible?: boolean
}

export interface SymbolBrowsePage {
  generation: number
  axf_path: string
  fingerprint: AxfFingerprint
  parent: string | null
  nodes: SymbolBrowseNode[]
}

export interface SymbolSearchResult {
  name: string
  address: number
  type: string
  size: number
  descriptor: SymbolDescriptor
}

export interface AxfFingerprint {
  size: number
  mtime_ns: number
}

export interface SymbolCatalogPage {
  generation: number
  axf_path: string
  parsed_at: number
  fingerprint: AxfFingerprint
  stale: boolean
  total: number
  items: SymbolDescriptor[]
  truncated_roots: string[]
  containers: SymbolContainerDescriptor[]
  browse_roots?: SymbolBrowseNode[]
}

export interface SymbolCatalogStatus {
  loaded: boolean
  generation: number
  axf_path: string
  parsed_at: number
  fingerprint: AxfFingerprint
  stale: boolean
  total: number
  container_count: number
  truncated_roots: string[]
}

export interface SymbolRebindSummary {
  preserved: string[]
  updated: string[]
  removed: string[]
}

export interface SymbolCLayoutResult {
  layout: {
    type_name: string
    size: number
    alignment: number
    pack: number | null
    leaf_count: number
  }
  rebind: SymbolRebindSummary
  generation: number
  axf_path: string
  total: number
  container_count: number
}

export interface SuperWatchWriteResult {
  path: string
  generation: number
  value: number | boolean
  verified: boolean
}

export interface SuperWatchTransactionDetail {
  code: 'superwatch_transaction_failed'
  phase: 'stop' | 'write' | 'readback' | 'reparse' | 'rebind' | 'restore'
  message: string
}

// Memory read result
export interface MemoryReadResult {
  address: string
  size: number
  data_base64: string
  data_hex: string
}

// Serial monitor event
export interface SerialEvent {
  event: string
  timestamp?: string
  port?: string
  direction?: 'RX' | 'TX'
  raw_hex?: string
  ascii?: string
  fields?: Record<string, { value: string; unit: string }>
  crc_valid?: boolean | null
}

// Modbus register snapshot
export interface ModbusRegisterValue {
  value: number
  name: string
  type: string
}

export interface ModbusSnapshot {
  _t: number
  registers: Record<number, ModbusRegisterValue>
}

// Project history entry
export interface ProjectHistoryEntry {
  path: string
  name: string
  last_used: string
}

// Full project history
export interface ProjectHistory {
  last_project: string | null
  history: ProjectHistoryEntry[]
}

// 探针自身固件版本检查
export interface FirmwareInfo {
  name: string
  version: string
  model: 'V3' | 'V4'
  family: 'microlink' | 'hpmlink'
  path: string
  source: 'github' | 'gitee' | 'local'
}

export type ProbeFirmwareCheckStatus =
  | 'ok'
  | 'upgrade_required'
  | 'no_firmware'
  | 'manifest_unavailable'
  | 'skipped'

export interface ProbeFirmwareCheck {
  status: ProbeFirmwareCheckStatus
  current_version: string | null
  min_required_version: string | null
  recommended_uf2: FirmwareInfo | null
  all_uf2s: FirmwareInfo[]
  firmware_dir: string | null
  instructions: string
}

export type ProbeFirmwareUpgradeStatus =
  | 'up_to_date'
  | 'updated'
  | 'copied_unverified'
  | 'manual_required'
  | 'no_probe_disk'
  | 'no_firmware'

export interface ProbeFirmwareUpgrade {
  status: ProbeFirmwareUpgradeStatus
  current_version?: string
  latest_version?: string
  verified_version?: string | null
  firmware?: string
  model?: 'V3' | 'V4'
  family?: 'microlink' | 'hpmlink'
  download_available?: boolean
  source?: 'github' | 'gitee' | 'local'
  message?: string
  stopped?: string[]
}

export interface ProbeFirmwareDownload {
  blob: Blob
  filename: string
  version: string
  source: 'github' | 'gitee' | 'local'
  family: 'microlink' | 'hpmlink'
}

export type FileSourceKind = 'symbol' | 'map'

export interface UploadedFileSource {
  path: string
  name: string
  size: number
  sha256: string
}
