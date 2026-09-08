import { describe, expect, it } from 'vitest'
import {
  API_BASE,
  WS_BASE,
  applyBackendEndpoint,
  applyReportedBackendPort,
  browserRuntimeBase,
  browserRuntimePort,
  isTauriRuntime,
  resolveRuntimeBase,
  runtimeBackendPort,
} from './runtimeEndpoint'

describe('runtime endpoint selection', () => {
  it('keeps the serving prefix for nested browser deployments', () => {
    expect(browserRuntimeBase('https://example.test/apps/probe/content/#/flash')).toBe('/apps/probe/content')
    expect(browserRuntimeBase('https://example.test/apps/probe/content/?build=123#/dashboard?tab=superwatch')).toBe('/apps/probe/content')
    expect(browserRuntimeBase('https://example.test/apps/probe/content/index.html?build=123#/flash')).toBe('/apps/probe/content')
    expect(browserRuntimeBase('http://127.0.0.1:8765/#/flash')).toBe('')
    expect(browserRuntimeBase('http://127.0.0.1:8765/index.html')).toBe('')
  })
  it('uses the current origin for a browser-hosted Web GUI', () => {
    expect(isTauriRuntime({})).toBe(false)
    expect(resolveRuntimeBase('http://127.0.0.1:8765/', false)).toBe('')
    expect(browserRuntimePort({ protocol: 'http:', port: '8765' })).toBe(8765)
    expect(browserRuntimePort({ protocol: 'https:', port: '' })).toBe(443)
  })

  it('uses the configured sidecar endpoint inside Tauri', () => {
    expect(isTauriRuntime({ __TAURI_INTERNALS__: {} })).toBe(true)
    expect(resolveRuntimeBase('http://127.0.0.1:8765/', true)).toBe('http://127.0.0.1:8765')
  })

  it('accepts the legacy Tauri marker used by older WebViews', () => {
    expect(isTauriRuntime({ __TAURI__: {} })).toBe(true)
  })

  it('applies one runtime port to REST and WebSocket transports', () => {
    applyBackendEndpoint({ port: 8766, instanceId: 'instance-b' })
    expect(API_BASE).toBe('http://127.0.0.1:8766')
    expect(WS_BASE).toBe('ws://127.0.0.1:8766')
    expect(runtimeBackendPort.value).toBe(8766)
  })

  it('uses a server-reported listener port without changing proxy transports', () => {
    const apiBase = API_BASE
    const wsBase = WS_BASE
    applyReportedBackendPort(8765)
    applyReportedBackendPort(0)
    applyReportedBackendPort('8766')
    expect(runtimeBackendPort.value).toBe(8765)
    expect(API_BASE).toBe(apiBase)
    expect(WS_BASE).toBe(wsBase)
  })
})
