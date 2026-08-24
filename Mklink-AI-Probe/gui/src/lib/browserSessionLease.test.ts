import { beforeEach, describe, expect, it, vi } from 'vitest'
import { browserSessionSocketUrl, startBrowserSessionLease } from './browserSessionLease'

describe('browser session lease', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  it('uses the serving backend and selects the matching WebSocket protocol', () => {
    expect(browserSessionSocketUrl(
      { protocol: 'http:', host: '127.0.0.1:8766' } as Location,
      'tab one',
    )).toBe('ws://127.0.0.1:8766/ws/browser-session?client_id=tab%20one')
    expect(browserSessionSocketUrl(
      { protocol: 'https:', host: 'probe.example' } as Location,
      'tab',
    )).toMatch(/^wss:/)
  })

  it('does nothing when browser ownership is disabled', () => {
    const stop = startBrowserSessionLease(false)
    expect(() => stop()).not.toThrow()
  })

  it('releases the device and browser lease when the page is closed', () => {
    const sendBeacon = vi.fn(() => true)
    vi.stubGlobal('navigator', { sendBeacon })
    class FakeWebSocket {
      addEventListener() {}
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)

    const stop = startBrowserSessionLease(true)
    window.dispatchEvent(new Event('pagehide'))
    stop()

    expect(sendBeacon).toHaveBeenCalledTimes(1)
    expect(sendBeacon.mock.calls.map(call => call[0])).toEqual([
      '/api/browser-session/release',
    ])
  })

  it('falls back to a keepalive request when sendBeacon cannot queue', async () => {
    const sendBeacon = vi.fn(() => false)
    const fetch = vi.fn(() => Promise.resolve(new Response()))
    vi.stubGlobal('navigator', { sendBeacon })
    vi.stubGlobal('fetch', fetch)
    class FakeWebSocket {
      addEventListener() {}
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)

    const stop = startBrowserSessionLease(true)
    stop()
    await Promise.resolve()

    expect(sendBeacon).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith(
      '/api/browser-session/release',
      expect.objectContaining({ method: 'POST', keepalive: true }),
    )
  })
})
