export const DESKTOP_SETTINGS_STORAGE_KEY = 'mklink.desktop.settings.v1'
export const DESKTOP_SETTINGS_CHANGED_EVENT = 'mklink:desktop-settings-changed'
export const DESKTOP_SETTINGS_VERSION = 1 as const
export const MAX_SEND_HISTORY = 20

export type RttTransmitMode = 'text' | 'hex'
export type RttLineEnding = '' | '\r' | '\n' | '\r\n'
export type RttEncoding = 'utf-8' | 'gb2312' | 'gbk' | 'gb18030' | 'big5'

export interface RttSendHistoryEntry {
  text: string
  mode: RttTransmitMode
  lineEnding: RttLineEnding
  timestamp: number
}

export interface DesktopSettings {
  version: 1
  symbolPath: string
  symbolDisplayPath?: string
  rttAddress: string
  rttEncoding: RttEncoding
  transmitMode: RttTransmitMode
  lineEnding: RttLineEnding
  sendHistory: RttSendHistoryEntry[]
}

export interface DesktopSettingsStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

export type SuccessfulSend = Omit<RttSendHistoryEntry, 'timestamp'>

export function isSymbolFilePath(path: string): boolean {
  return /\.(axf|elf|out)$/i.test(path.trim())
}

export function isSameFileSourcePath(
  left: string | null | undefined,
  right: string | null | undefined,
): boolean {
  if (!left?.trim() || !right?.trim()) return false
  const normalize = (value: string) => {
    const path = value.trim().replace(/\\/g, '/')
    return /^[a-z]:\//i.test(path) ? path.toLowerCase() : path
  }
  return normalize(left) === normalize(right)
}

function defaults(): DesktopSettings {
  return {
    version: DESKTOP_SETTINGS_VERSION,
    symbolPath: '',
    symbolDisplayPath: '',
    rttAddress: '',
    rttEncoding: 'utf-8',
    transmitMode: 'text',
    lineEnding: '',
    sendHistory: [],
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isTransmitMode(value: unknown): value is RttTransmitMode {
  return value === 'text' || value === 'hex'
}

function isLineEnding(value: unknown): value is RttLineEnding {
  return value === '' || value === '\r' || value === '\n' || value === '\r\n'
}

function isRttAddress(value: unknown): value is string {
  return value === '' || (typeof value === 'string' && /^0x[0-9a-f]+$/i.test(value))
}

function isRttEncoding(value: unknown): value is RttEncoding {
  return value === 'utf-8' || value === 'gb2312' || value === 'gbk'
    || value === 'gb18030' || value === 'big5'
}

function parseHistoryEntry(value: unknown): RttSendHistoryEntry | null {
  if (!isRecord(value)) return null
  if (
    typeof value.text !== 'string'
    || !isTransmitMode(value.mode)
    || !isLineEnding(value.lineEnding)
    || typeof value.timestamp !== 'number'
    || !Number.isFinite(value.timestamp)
  ) return null
  return {
    text: value.text,
    mode: value.mode,
    lineEnding: value.lineEnding,
    timestamp: value.timestamp,
  }
}

function normalize(value: unknown): DesktopSettings {
  if (!isRecord(value) || value.version !== DESKTOP_SETTINGS_VERSION) return defaults()
  const history = Array.isArray(value.sendHistory)
    ? value.sendHistory
      .map(parseHistoryEntry)
      .filter((entry): entry is RttSendHistoryEntry => entry !== null)
      .slice(0, MAX_SEND_HISTORY)
    : []
  return {
    version: DESKTOP_SETTINGS_VERSION,
    symbolPath: typeof value.symbolPath === 'string' ? value.symbolPath : '',
    symbolDisplayPath: typeof value.symbolDisplayPath === 'string' ? value.symbolDisplayPath : '',
    rttAddress: isRttAddress(value.rttAddress) ? value.rttAddress : '',
    rttEncoding: isRttEncoding(value.rttEncoding) ? value.rttEncoding : 'utf-8',
    transmitMode: isTransmitMode(value.transmitMode) ? value.transmitMode : 'text',
    lineEnding: isLineEnding(value.lineEnding) ? value.lineEnding : '',
    sendHistory: history,
  }
}

export function loadDesktopSettings(storage: DesktopSettingsStorage): DesktopSettings {
  try {
    const raw = storage.getItem(DESKTOP_SETTINGS_STORAGE_KEY)
    return raw === null ? defaults() : normalize(JSON.parse(raw))
  } catch {
    return defaults()
  }
}

export function saveDesktopSettings(
  storage: DesktopSettingsStorage,
  settings: DesktopSettings,
): DesktopSettings {
  const saved = normalize(settings)
  storage.setItem(DESKTOP_SETTINGS_STORAGE_KEY, JSON.stringify(saved))
  if (typeof window !== 'undefined' && storage === window.localStorage) {
    window.dispatchEvent(new CustomEvent(DESKTOP_SETTINGS_CHANGED_EVENT))
  }
  return normalize(saved)
}

export function recordSuccessfulSend(
  storage: DesktopSettingsStorage,
  send: SuccessfulSend,
): DesktopSettings {
  const settings = loadDesktopSettings(storage)
  const previous = settings.sendHistory[0]
  if (
    previous?.text === send.text
    && previous.mode === send.mode
    && previous.lineEnding === send.lineEnding
  ) return settings

  settings.sendHistory = [
    { ...send, timestamp: Date.now() },
    ...settings.sendHistory,
  ].slice(0, MAX_SEND_HISTORY)
  return saveDesktopSettings(storage, settings)
}
