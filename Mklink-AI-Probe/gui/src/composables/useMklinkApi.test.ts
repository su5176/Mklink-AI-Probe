import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMklinkApi } from './useMklinkApi'

describe('RTT API contracts', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({}),
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  it('passes an optional explicit source path to RTT detection', async () => {
    const api = useMklinkApi()
    await api.findRtt('C:\\firmware\\app.elf')
    await api.findRtt()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/rtt-find', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ source_path: 'C:\\firmware\\app.elf' }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/rtt-find', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({}),
    }))
  })

  it('writes RTT bytes through a compact lowercase hex payload', async () => {
    const api = useMklinkApi()
    await api.writeRtt(Uint8Array.of(0x00, 0x0a, 0xff))

    expect(fetchMock).toHaveBeenCalledWith('/api/dash/rtt/write', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ data_hex: '000aff' }),
    }))
  })

  it('keeps AXF parsing built-in by default and forwards explicit external mode', async () => {
    const api = useMklinkApi()
    await api.parseAxf('C:\\firmware\\app.axf')
    await api.parseAxf('C:\\firmware\\app.axf', 'external')

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/device/parse-axf', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ axf: 'C:\\firmware\\app.axf' }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/device/parse-axf', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ axf: 'C:\\firmware\\app.axf', elf_backend: 'external' }),
    }))
  })

  it('switches the shared RTT decoder encoding', async () => {
    const api = useMklinkApi()
    await api.setRttEncoding('gb18030')

    expect(fetchMock).toHaveBeenCalledWith('/api/dash/rtt/encoding', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ encoding: 'gb18030' }),
    }))
  })

  it('forwards the guarded probe voltage request exactly', async () => {
    const api = useMklinkApi()
    await api.setPowerOn(5000, true)

    expect(fetchMock).toHaveBeenCalledWith('/api/device/power', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ voltage_mv: 5000, confirm_5v: true }),
    }))
  })

  it('refreshes connection state after rebooting the probe', async () => {
    const api = useMklinkApi()
    await api.rebootProbe()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/device/reboot', expect.objectContaining({
      method: 'POST',
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/device/status', expect.any(Object))
  })

  it('surfaces structured backend symbol-source errors', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      statusText: 'Conflict',
      json: async () => ({
        detail: {
          code: 'symbol_source_mismatch',
          message: 'requested AXF was not activated',
        },
      }),
    })

    await expect(useMklinkApi().parseAxf('C:\\firmware\\next.axf'))
      .rejects.toThrow('requested AXF was not activated')
  })

  it('uploads a browser-selected symbol file as multipart data', async () => {
    const api = useMklinkApi()
    const file = new File(['ELF'], 'firmware.axf', { type: 'application/octet-stream' })

    await api.uploadFileSource('symbol', file)

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/files/symbol')
    expect(options.method).toBe('POST')
    expect(options.body).toBeInstanceOf(FormData)
    expect(new Headers(options.headers).has('Content-Type')).toBe(false)
    expect((options.body as FormData).get('file')).toBe(file)
  })
})
