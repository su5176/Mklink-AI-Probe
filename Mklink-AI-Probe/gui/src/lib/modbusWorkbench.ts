export const MODBUS_SETTINGS_KEY = 'mklink.modbus-workbench.settings.v1'

export const READ_FUNCTIONS = new Set([1, 2, 3, 4])
export const BIT_FUNCTIONS = new Set([1, 2, 5, 15])

export const FUNCTION_OPTIONS = [
  { fc: 1, zh: '读线圈', en: 'Read Coils' },
  { fc: 2, zh: '读离散输入', en: 'Read Discrete Inputs' },
  { fc: 3, zh: '读保持寄存器', en: 'Read Holding Registers' },
  { fc: 4, zh: '读输入寄存器', en: 'Read Input Registers' },
  { fc: 5, zh: '写单线圈', en: 'Write Single Coil' },
  { fc: 6, zh: '写单寄存器', en: 'Write Single Register' },
  { fc: 15, zh: '写多线圈', en: 'Write Multiple Coils' },
  { fc: 16, zh: '写多寄存器', en: 'Write Multiple Registers' },
] as const

export interface ModbusSettings {
  port: string
  slave: number
  baudrate: number
  bytesize: number
  parity: string
  stopbits: number
  timeout: number
  retries: number
  localEcho: boolean
  fc: number
  start: string
  quantity: number
  values: string
  loopIntervalMs: number
  loopCount: number
}

export const DEFAULT_MODBUS_SETTINGS: ModbusSettings = {
  port: '', slave: 1, baudrate: 115200, bytesize: 8, parity: 'N', stopbits: 1,
  timeout: 0.5, retries: 0, localEcho: false, fc: 3, start: '0', quantity: 10,
  values: '0', loopIntervalMs: 1000, loopCount: 0,
}

export function parseModbusInteger(value: unknown, label: string): number {
  const text = String(value ?? '').trim()
  if (!/^(?:0[xX][0-9a-fA-F]+|\d+)$/.test(text)) throw new Error(`${label} must be a decimal or 0x hexadecimal integer`)
  const parsed = Number.parseInt(text, text.toLowerCase().startsWith('0x') ? 16 : 10)
  if (!Number.isSafeInteger(parsed)) throw new Error(`${label} is out of range`)
  return parsed
}

export function parseModbusValues(text: string, bitValues: boolean): Array<number | boolean> {
  const tokens = text.split(/[\s,;]+/).map(value => value.trim()).filter(Boolean)
  if (!tokens.length) throw new Error('At least one value is required')
  return tokens.map((token) => {
    if (bitValues) {
      const normalized = token.toLowerCase()
      if (['1', 'true', 'on'].includes(normalized)) return true
      if (['0', 'false', 'off'].includes(normalized)) return false
      throw new Error(`Invalid coil value: ${token}`)
    }
    const value = parseModbusInteger(token, 'Register value')
    if (value < 0 || value > 0xffff) throw new Error('Register value must be in the range 0..65535')
    return value
  })
}

export function buildTransaction(settings: ModbusSettings) {
  const fc = Number(settings.fc)
  const start = parseModbusInteger(settings.start, 'Start address')
  if (start < 0 || start > 0xffff) throw new Error('Start address must be in the range 0..65535')
  if (READ_FUNCTIONS.has(fc)) return { fc, start, quantity: Number(settings.quantity), values: null }
  return { fc, start, quantity: null, values: parseModbusValues(settings.values, BIT_FUNCTIONS.has(fc)) }
}

export function loadModbusSettings(storage: Pick<Storage, 'getItem'> | null): ModbusSettings {
  if (!storage) return { ...DEFAULT_MODBUS_SETTINGS }
  try {
    const raw = storage.getItem(MODBUS_SETTINGS_KEY)
    return raw ? { ...DEFAULT_MODBUS_SETTINGS, ...JSON.parse(raw) } : { ...DEFAULT_MODBUS_SETTINGS }
  } catch { return { ...DEFAULT_MODBUS_SETTINGS } }
}
