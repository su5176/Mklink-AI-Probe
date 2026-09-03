import { describe, expect, it } from 'vitest'
import { buildTransaction, DEFAULT_MODBUS_SETTINGS, loadModbusSettings, parseModbusValues } from './modbusWorkbench'

describe('Modbus workbench request parsing', () => {
  it('accepts decimal and hexadecimal register values', () => {
    expect(parseModbusValues('1, 0x1234 65535', false)).toEqual([1, 0x1234, 65535])
  })

  it('accepts explicit coil states', () => {
    expect(parseModbusValues('ON, 0, true, off', true)).toEqual([true, false, true, false])
  })

  it('builds a read request with a hexadecimal address', () => {
    expect(buildTransaction({ ...DEFAULT_MODBUS_SETTINGS, fc: 4, start: '0x10', quantity: 8 })).toEqual({
      fc: 4, start: 16, quantity: 8, values: null,
    })
  })

  it('falls back to defaults when persisted settings are malformed', () => {
    expect(loadModbusSettings({ getItem: () => '{bad-json' })).toEqual(DEFAULT_MODBUS_SETTINGS)
  })
})
