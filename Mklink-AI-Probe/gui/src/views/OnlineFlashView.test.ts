import { flushPromises, mount, shallowMount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, expectTypeOf, it, vi } from 'vitest'
import { reactive, ref } from 'vue'
import App from '../App.vue'
import router from '../router'
import type { JobAction, JobRequest, PackOperationResponse } from '../types/onlineFlash'
import DashboardView from './DashboardView.vue'
import FlashLogPanel from '../components/online-flash/FlashLogPanel.vue'
import FirmwareWorkspace from '../components/online-flash/FirmwareWorkspace.vue'
import TargetPackPanel from '../components/online-flash/TargetPackPanel.vue'
import actionBarSource from '../components/online-flash/FlashActionBar.vue?raw'
import logPanelSource from '../components/online-flash/FlashLogPanel.vue?raw'
import firmwareWorkspaceSource from '../components/online-flash/FirmwareWorkspace.vue?raw'
import onlineFlashViewSource from './OnlineFlashView.vue?raw'
import probeSettingsSource from '../components/online-flash/ProbeSettingsPanel.vue?raw'

async function onlineFlashView() {
  const path = './OnlineFlashView.vue'
  return (await import(/* @vite-ignore */ path)).default
}

async function onlineFlashApi() {
  const path = '../composables/useOnlineFlashApi'
  return (await import(/* @vite-ignore */ path)).useOnlineFlashApi()
}

async function onlineFlashApiModule() {
  const path = '../composables/useOnlineFlashApi'
  return import(/* @vite-ignore */ path)
}

vi.mock('../composables/useMklinkApi', () => ({
  useMklinkApi: () => ({
    deviceStatus: reactive({ connected: true }),
    startStatusPolling: vi.fn(),
    stopStatusPolling: vi.fn(),
    flashDevice: vi.fn(),
    resetDevice: vi.fn(),
    eraseDevice: vi.fn(),
    haltDevice: vi.fn(),
    resumeDevice: vi.fn(),
  }),
}))

vi.mock('../composables/useBackendHealth', () => ({
  useBackendHealth: () => ({
    backendState: ref('alive'),
    startHealthPolling: vi.fn(),
    stopHealthPolling: vi.fn(),
    isTauri: true,
  }),
}))

vi.mock('../composables/useToast', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}))

vi.mock('../composables/useResourceStatus', () => ({
  useResourceStatus: () => ({ refresh: vi.fn(), getBridgeOwner: () => '' }),
}))

class FakeEventSource {
  static instances: FakeEventSource[] = []
  readonly listeners = new Map<string, Array<(event: Event) => void>>()
  closed = false

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, listener: (event: Event) => void) {
    const listeners = this.listeners.get(name) ?? []
    listeners.push(listener)
    this.listeners.set(name, listeners)
  }

  emit(name: string, data: unknown) {
    if (this.closed) return
    const event = new MessageEvent(name, { data: JSON.stringify(data) })
    for (const listener of this.listeners.get(name) ?? []) listener(event)
  }

  emitNativeError() {
    if (this.closed) return
    const event = new Event('error')
    for (const listener of this.listeners.get('error') ?? []) listener(event)
  }

  close() {
    this.closed = true
  }
}

const dashStub = { template: '<div />', props: ['deviceConnected'] }

describe('online flash navigation and workspace', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn(), clear: vi.fn() })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const value = url.endsWith('/packs/status')
        ? { last_error: null, index_available: false, target_count: 0 }
        : []
      return new Response(JSON.stringify(value), { status: 200 })
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('registers the hash-router online flash route', () => {
    const route = router.getRoutes().find(candidate => candidate.name === 'online-flash')

    expect(route?.path).toBe('/online-flash')
  })

  it('shows 脱机烧录 beside 在线烧录 in the top navigation and navigates to it', async () => {
    await router.push('/config')
    await router.isReady()
    const wrapper = mount(App, {
      global: {
        plugins: [router],
        stubs: { RouterView: true, StatusBar: true, ToastContainer: true },
      },
    })

    const labels = wrapper.findAll('.nav-tab').map(tab => tab.text())
    expect(labels).toEqual(['配置', '仪表盘', '脱机烧录', '在线烧录', '现场 Agent'])

    await wrapper.findAll('.nav-tab')[2].trigger('click')
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('offline-flash'))
    wrapper.unmount()
  })

  it('removes the legacy dashboard 脱机烧录 tab', async () => {
    await router.push('/dashboard')
    const wrapper = shallowMount(DashboardView, {
      global: {
        plugins: [router],
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          VofaTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })
    try {
      const labels = wrapper.findAll('.tab-btn').map(tab => tab.text())
      expect(labels).not.toContain('脱机烧录')
    } finally {
      wrapper.unmount()
    }
  })

  it('mounts the stable four-zone workspace landmarks', async () => {
    const wrapper = mount(await onlineFlashView())

    expect(wrapper.find('.online-flash-grid').exists()).toBe(true)
    expect(wrapper.find('aside[data-zone="settings"]').exists()).toBe(true)
    expect(wrapper.find('main[data-zone="firmware"]').exists()).toBe(true)
    expect(wrapper.find('aside[data-zone="flash-map"]').exists()).toBe(true)
    expect(wrapper.find('section[data-zone="logs"]').exists()).toBe(true)
  })

  it('renders the firmware workspace as the only main landmark', async () => {
    await router.push('/online-flash')
    const wrapper = mount(App, {
      global: {
        plugins: [router],
        stubs: { StatusBar: true, ToastContainer: true },
      },
    })
    try {
      await vi.waitFor(() => expect(wrapper.find('main[data-zone="firmware"]').exists()).toBe(true))
      expect(wrapper.findAll('main')).toHaveLength(1)
    } finally {
      wrapper.unmount()
    }
  })
})

describe('useOnlineFlashApi', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('fetch', vi.fn())
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('does not make a request until a client method is called', async () => {
    await onlineFlashApi()

    expect(fetch).not.toHaveBeenCalled()
  })

  it('encodes target search filters in the request URL', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('[]', { status: 200 }))
    const api = await onlineFlashApi()

    await api.searchTargets('hpm 53', { vendor: 'HPMicro & Co', installed: true, limit: 7 })

    const [url, options] = vi.mocked(fetch).mock.calls[0]
    expect(url).toBe('/api/online-flash/targets?q=hpm+53&vendor=HPMicro+%26+Co&installed=true&limit=7')
    expect(new Headers(options?.headers).get('Content-Type')).toBe('application/json')
  })

  it('loads target flash sector geometry independently of firmware inspection', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify([{
      name: 'flash', start: 0x08000000, length: 0x80000, sector_size: 0x800,
    }]), { status: 200 }))
    const api = await onlineFlashApi()

    const regions = await api.getTargetMemoryMap('STM32F103 RC')

    expect(fetch).toHaveBeenCalledWith(
      '/api/online-flash/targets/STM32F103%20RC/memory-map',
      expect.any(Object),
    )
    expect(regions[0]?.sector_size).toBe(0x800)
  })

  it('uses multipart FormData without forcing a JSON content type', async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response('{}', { status: 200 }))
    const api = await onlineFlashApi()
    const pack = new File(['pack'], 'device.pack')
    const image = new File(['firmware'], 'firmware.bin')

    await api.importPack(pack)
    await api.inspectImage(image, 'HPM5300', 0x1000)
    await api.addCustomFlm(new File(['flm'], 'external.flm'), 'HPM5300')

    const [importUrl, importOptions] = vi.mocked(fetch).mock.calls[0]
    const [inspectUrl, inspectOptions] = vi.mocked(fetch).mock.calls[1]
    const [flmUrl, flmOptions] = vi.mocked(fetch).mock.calls[2]
    expect(importUrl).toBe('/api/online-flash/packs/import')
    expect(inspectUrl).toBe('/api/online-flash/images/inspect')
    expect(flmUrl).toBe('/api/online-flash/algorithms')
    expect(importOptions?.body).toBeInstanceOf(FormData)
    expect(inspectOptions?.body).toBeInstanceOf(FormData)
    expect(flmOptions?.body).toBeInstanceOf(FormData)
    expect(new Headers(importOptions?.headers).has('Content-Type')).toBe(false)
    expect(new Headers(inspectOptions?.headers).has('Content-Type')).toBe(false)
    expect(new Headers(flmOptions?.headers).has('Content-Type')).toBe(false)
  })

  it('offers the supported 10 MHz online SWD frequency', () => {
    expect(probeSettingsSource).toContain('<option :value="10000000">10 MHz</option>')
  })

  it('locks target selection while a custom algorithm mutation is active', async () => {
    const source = await import('../components/online-flash/TargetPackPanel.vue?raw')
    expect(source.default).toContain(':disabled="busy || algorithmBusy"')
  })

  it('marks HPM targets as ROM API devices without custom FLM controls', async () => {
    const wrapper = mount(TargetPackPanel, { props: {
      targets: [], selectedPart: 'HPM5300', status: null, busy: false,
      cancelPending: false, progress: 0, phase: 'preparing', error: '',
      algorithms: [], algorithmBusy: false, algorithmError: '',
      canManageAlgorithms: false, algorithmNotRequired: true,
    } })

    expect(wrapper.text()).toContain('HPM ROM API')
    expect(wrapper.find('[data-testid="custom-flm-input"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('fills the target search field as soon as target selection begins', async () => {
    const target = { ...installedTarget, part_number: 'STM32H7B0VBT6' }
    const wrapper = mount(TargetPackPanel, { props: {
      targets: [target], selectedPart: target.part_number, status: null, busy: false,
      cancelPending: false, progress: 0, phase: 'preparing', error: '',
      algorithms: [], algorithmBusy: false, algorithmError: '',
      canManageAlgorithms: true, algorithmNotRequired: false,
    } })

    await wrapper.get(`[data-testid="target-${target.part_number}"]`).trigger('click')

    expect(wrapper.get<HTMLInputElement>('[data-testid="target-search"]').element.value)
      .toBe(target.part_number)
    wrapper.unmount()
  })

  it('forwards preview abort signals to fetch', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
    const controller = new AbortController()
    const api = await onlineFlashApi()

    await api.previewImage('image', 0, 4096, controller.signal)

    expect(vi.mocked(fetch).mock.calls[0][1]?.signal).toBe(controller.signal)
  })

  it('posts job JSON and addresses job endpoints', async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response('{}', { status: 200 }))
    const api = await onlineFlashApi()
    const request = {
      actions: ['connect', 'disconnect'],
      probe_id: 'probe/1',
      target_part: 'HPM5300',
    }

    await api.createJob(request)
    await api.getActiveJob()
    await api.getJob('job/1')
    await api.stopJob('job/1')

    expect(vi.mocked(fetch).mock.calls.map(([url]) => url)).toEqual([
      '/api/online-flash/jobs',
      '/api/online-flash/jobs/active',
      '/api/online-flash/jobs/job%2F1',
      '/api/online-flash/jobs/job%2F1/stop',
    ])
    expect(vi.mocked(fetch).mock.calls[0][1]).toEqual(expect.objectContaining({
      method: 'POST',
      body: JSON.stringify(request),
    }))
    expectTypeOf<JobRequest['actions'][number]>().toEqualTypeOf<JobAction>()
  })

  it('preserves structured API conflict details', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({
      detail: { code: 'PROBE_BUSY', owner: 'ai-session', resource: 'TARGET_DEBUG' },
    }), { status: 409, statusText: 'Conflict' }))
    const { OnlineFlashApiError, useOnlineFlashApi } = await onlineFlashApiModule()

    const error = await useOnlineFlashApi().listProbes().catch((value: unknown) => value)

    expect(error).toBeInstanceOf(OnlineFlashApiError)
    expect(error).toMatchObject({
      status: 409,
      code: 'PROBE_BUSY',
      owner: 'ai-session',
      resource: 'TARGET_DEBUG',
      detail: { code: 'PROBE_BUSY', owner: 'ai-session', resource: 'TARGET_DEBUG' },
    })
    expect(error.message).toBe('PROBE_BUSY: TARGET_DEBUG is owned by ai-session')
  })

  it('formats nested API details as stable JSON instead of object coercion', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({
      detail: { validation: { reason: 'invalid', field: 'base_address' } },
    }), { status: 422, statusText: 'Unprocessable Entity' }))
    const { OnlineFlashApiError, useOnlineFlashApi } = await onlineFlashApiModule()

    const error = await useOnlineFlashApi().listProbes().catch((value: unknown) => value)

    expect(error).toBeInstanceOf(OnlineFlashApiError)
    expect(error.message).toBe('{"validation":{"field":"base_address","reason":"invalid"}}')
  })

  it('returns consumable adapter and default-worker Pack variants', async () => {
    const fixtures = [
      {
        result: { status: 'installed', part_number: 'STM32F103RC' },
        events: [{ type: 'progress', progress: 0.5 }],
      },
      {
        result: { status: 'installed', pack_id: 'Keil.STM32F1xx_DFP', version: '2.4.1' },
        events: [{ type: 'progress', current: 1, total: 2 }],
      },
      {
        result: { status: 'updated' },
        events: [{ type: 'log', message: 'updated' }],
      },
      {
        result: { status: 'updated', target_count: 42 },
        events: [],
      },
    ] satisfies PackOperationResponse[]
    const pending = [...fixtures]
    vi.mocked(fetch).mockImplementation(async () => (
      new Response(JSON.stringify(pending.shift()), { status: 200 })
    ))
    const api = await onlineFlashApi()

    const responses = [
      await api.installPack('STM32F103RC'),
      await api.installPack('STM32F103RC'),
      await api.updatePackIndex(),
      await api.updatePackIndex(),
    ]

    expect(responses.map(consumePackResponse)).toEqual([
      ['0.5', 'STM32F103RC'],
      ['1/2', 'Keil.STM32F1xx_DFP@2.4.1'],
      ['updated', 'updated'],
      ['42'],
    ])
  })

  it('delivers streamed Pack progress before the operation result settles', async () => {
    const encoder = new TextEncoder()
    let finish!: () => void
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(`${JSON.stringify({
          type: 'event',
          event: { type: 'progress', progress: 0.25, phase: 'downloading' },
        })}\n`))
        finish = () => {
          controller.enqueue(encoder.encode(`${JSON.stringify({
            type: 'result',
            result: { status: 'installed', pack_id: 'Keil.STM32F1xx_DFP', version: '2.4.1' },
          })}\n`))
          controller.close()
        }
      },
    })
    vi.mocked(fetch).mockResolvedValue(new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'application/x-ndjson' },
    }))
    const { useOnlineFlashApi } = await onlineFlashApiModule()
    const events: unknown[] = []
    let settled = false

    const pending = useOnlineFlashApi().installPack('STM32F103RC', event => {
      events.push(event)
    }).finally(() => { settled = true })

    await vi.waitFor(() => expect(events).toEqual([
      { type: 'progress', progress: 0.25, phase: 'downloading' },
    ]))
    expect(settled).toBe(false)

    finish()
    await expect(pending).resolves.toMatchObject({
      result: { status: 'installed', pack_id: 'Keil.STM32F1xx_DFP' },
    })
  })

  it('bounds retained Pack event history while delivering every live callback', async () => {
    const encoder = new TextEncoder()
    const lines = Array.from({ length: 200 }, (_, index) => JSON.stringify({
      type: 'event', event: { type: 'progress', progress: index / 199 },
    }))
    lines.push(JSON.stringify({
      type: 'result', result: { status: 'updated', target_count: 1 },
    }))
    vi.mocked(fetch).mockResolvedValue(new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(`${lines.join('\n')}\n`))
          controller.close()
        },
      }),
      { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } },
    ))
    const delivered: unknown[] = []

    const response = await (await onlineFlashApi()).updatePackIndex(event => delivered.push(event))

    expect(delivered).toHaveLength(200)
    expect(response.events).toHaveLength(128)
    expect(response.events[0]).toMatchObject({ progress: 72 / 199 })
  })

  it('filters replayed sequences and closes synchronously after a terminal event', async () => {
    const onEvent = vi.fn()
    const subscription = (await onlineFlashApi()).subscribeJob('job/1', 12, onEvent)
    const source = FakeEventSource.instances[0]
    const progress = {
      job_id: 'job/1', sequence: 13, timestamp: 1, event: 'progress', message: '',
      state: null, progress: 0.5,
    }
    const terminal = {
      job_id: 'job/1', sequence: 14, timestamp: 2, event: 'state', message: '',
      state: 'succeeded', progress: 1,
    }

    expect(source.url).toBe('/api/online-flash/jobs/job%2F1/events?after=12')
    source.emit('progress', progress)
    source.emit('progress', progress)
    source.emit('state', terminal)
    source.emit('progress', { ...progress, sequence: 15 })
    expect(onEvent).toHaveBeenCalledTimes(2)
    expect(onEvent).toHaveBeenNthCalledWith(1, progress)
    expect(onEvent).toHaveBeenNthCalledWith(2, terminal)
    expect(source.closed).toBe(true)

    subscription.close()
    expect(source.closed).toBe(true)
  })

  it('closes after a server error event without enabling native reconnect', async () => {
    const onEvent = vi.fn()
    const subscription = (await onlineFlashApi()).subscribeJob('job/1', 0, onEvent)
    const eventSource = FakeEventSource.instances[0]

    eventSource.emit('error', { code: 'UNKNOWN_ERROR', message: 'event stream failed' })

    expect(onEvent).toHaveBeenCalledWith({ code: 'UNKNOWN_ERROR', message: 'event stream failed' })
    expect(eventSource.closed).toBe(true)
    expect(eventSource.listeners.get('error')).toHaveLength(1)
    subscription.close()
  })

  it('closes and reports a native connection error exactly once', async () => {
    const onEvent = vi.fn()
    const onError = vi.fn()
    ;(await onlineFlashApi()).subscribeJob('job/1', 7, onEvent, onError)
    const source = FakeEventSource.instances[0]

    source.emitNativeError()
    source.emitNativeError()

    expect(source.closed).toBe(true)
    expect(source.listeners.get('error')).toHaveLength(1)
    expect(onEvent).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith({
      code: 'STREAM_ERROR',
      message: 'Event stream connection failed',
    })
  })
})

function consumePackResponse(response: PackOperationResponse): string[] {
  const events = response.events.map(event => {
    if (event.type === 'log') return event.message
    if ('progress' in event) return String(event.progress)
    return `${event.current}/${event.total}`
  })
  const result = response.result
  if (result.status === 'installed') {
    if ('part_number' in result) return [...events, result.part_number]
    return [...events, `${result.pack_id}@${result.version}`]
  }
  return [...events, 'target_count' in result ? String(result.target_count) : 'updated']
}

const probeFixture = {
  unique_id: 'mklink-1', vendor_name: 'MuseLab', product_name: 'MKLink',
  description: 'MKLink CMSIS-DAP', vid: 0x34b7, pid: 0x0001, serial_number: 'ABC',
}

const installedTarget = {
  part_number: 'DEVICE_A', vendor: 'Vendor', pack_id: 'Vendor.Device_DFP',
  pack_version: '1.0.0', installed: true, source: 'installed',
}
const regularTarget = installedTarget
const hpmTarget = {
  part_number: 'HPM5300', vendor: 'HPMicro', pack_id: null,
  pack_version: null, installed: true, source: 'hpm-rom-api',
}

function viewFetch(targets = [installedTarget]) {
  const algorithms: Array<Record<string, unknown>> = []
  return vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 })
    if (url.endsWith('/probes')) return json([probeFixture])
    if (url.includes('/targets?')) return json(targets)
    if (url.includes('/targets/') && url.endsWith('/memory-map')) return json([{
      name: 'flash', start: 0x08000000, length: 0x80000, sector_size: 0x800,
    }])
    if (url.endsWith('/packs/status')) return json({ last_error: null, index_available: true, target_count: targets.length })
    if (url.includes('/algorithms?')) return json(algorithms)
    if (url.endsWith('/algorithms') && options?.method === 'POST') {
      const record = {
        algorithm_id: 'external-1', target_part: 'DEVICE_A', file_name: 'external.flm',
        flash_start: 0x90000000, flash_size: 0x800000, page_size: 0x1000,
        sector_sizes: [[0, 0x1000]],
      }
      algorithms.push(record)
      return json(record)
    }
    if (url.endsWith('/packs/install')) return json({
      result: { status: 'installed', part_number: JSON.parse(String(options?.body)).part_number },
      events: [{ type: 'progress', progress: 1 }],
    })
    if (url.endsWith('/packs/import')) return json({
      result: { status: 'installed', pack_id: 'Vendor.Device_DFP', version: '1.0.0' },
      events: [{ type: 'progress', progress: 1 }],
    })
    if (url.endsWith('/images/inspect')) return json({
      image_id: 'image-1', file_name: 'firmware.bin', format: 'bin', size: 32,
      sha256: 'abc123', start: 0x80000000, end: 0x80000020,
      segments: [{ start: 0x80000000, end: 0x80000020 }], base_address: 0x80000000,
      sector_operations_available: true,
      sectors: [{ address: 0x80000000, size: 0x1000 }],
    })
    if (url.endsWith('/memory/read-stream')) return new Response(new Uint8Array(32).fill(0x41), {
      status: 200,
      headers: { 'Content-Type': 'application/octet-stream' },
    })
    if (url.includes('/preview?')) return json({
      address: 0x80000000, length: 32, data_base64: btoa('\x41'.repeat(32)), present: Array(32).fill(true),
    })
    if (url.endsWith('/jobs') && options?.method === 'POST') return json({
      job_id: 'job-1',
      job: {
        job_id: 'job-1', state: 'queued', actions: ['program'], image_id: 'image-1',
        created_at: 1, updated_at: 1, probe_id: 'mklink-1', target_part: 'DEVICE_A',
        frequency: 1000000, connect_mode: 'halt', reset_mode: 'default', file_path: null,
        image_format: 'bin', image_start: 0x80000000, image_end: 0x80000020,
        image_size: 32, image_sha256: 'abc123', current_action: null, stage_progress: 0,
        total_progress: 0, speed_bytes_per_second: 0, elapsed_seconds: 0,
        error_code: null, error_message: null,
      },
    })
    if (url.endsWith('/jobs/job-1/stop')) return json({ state: 'stopping', job_id: 'job-1' })
    throw new Error(`Unexpected request: ${url}`)
  })
}

async function chooseFirmware(wrapper: ReturnType<typeof mount>, name = 'firmware.bin') {
  const input = wrapper.get('[data-testid="firmware-input"]')
  Object.defineProperty(input.element, 'files', {
    configurable: true,
    value: [new File(['firmware'], name)],
  })
  await input.trigger('change')
}

async function chooseCustomFlm(wrapper: ReturnType<typeof mount>) {
  const input = wrapper.get('[data-testid="custom-flm-input"]')
  await vi.waitFor(() => expect(input.attributes('disabled')).toBeUndefined())
  Object.defineProperty(input.element, 'files', {
    configurable: true,
    value: [new File(['algorithm'], 'external.flm')],
  })
  await input.trigger('change')
}

async function choosePack(wrapper: ReturnType<typeof mount>) {
  const input = wrapper.get('[data-testid="pack-import-input"]')
  Object.defineProperty(input.element, 'files', {
    configurable: true,
    value: [new File(['pack'], 'device.pack')],
  })
  await input.trigger('change')
}

async function readyAndStart(wrapper: ReturnType<typeof mount>) {
  await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
  await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
  await chooseFirmware(wrapper)
  await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
  await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
  await wrapper.get('[data-testid="start-job"]').trigger('click')
  await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
}

describe('online flash task workspace behavior', () => {
  it('defaults the connection mode to keeping the target running', async () => {
    const wrapper = mount(await onlineFlashView())

    expect(wrapper.get<HTMLSelectElement>('[data-testid="connect-mode"]').element.value)
      .toBe('attach')
    wrapper.unmount()
  })

  it('loads the selected target memory map before a firmware file is chosen', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))

    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')

    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([url]) => (
      String(url).endsWith('/targets/DEVICE_A/memory-map')
    ))).toBe(true))
    expect(wrapper.find('[data-testid="firmware-input"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('makes the flashing job available after reading target memory', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/targets/DEVICE_A/memory-map'))).toBe(true))

    await vi.waitFor(() => expect(wrapper.get('[data-testid="memory-read-submit"]').attributes('disabled')).toBeUndefined())
    const readButton = wrapper.get('[data-testid="memory-read-submit"]')
    await readButton.trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[data-testid="memory-read-address"]').exists()).toBe(true))
    await wrapper.get('[data-testid="memory-read-address"]').setValue('0x80000000')
    await wrapper.get('[data-testid="memory-read-end-address"]').setValue('0x80000020')
    await wrapper.get('[data-testid="memory-read-confirm"]').trigger('click')

    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/images/inspect'))).toBe(true))
    const inspectRequest = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/images/inspect'))
    expect((inspectRequest?.[1]?.body as FormData).get('captured_from_target')).toBe('true')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    expect(wrapper.text()).toContain('read-0x80000000-32.bin')
    wrapper.unmount()
  })

  it('keeps flash progress and logs visible after programming captured memory', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="memory-read-submit"]').attributes('disabled')).toBeUndefined())

    await wrapper.get('[data-testid="memory-read-submit"]').trigger('click')
    await wrapper.get('[data-testid="memory-read-address"]').setValue('0x80000000')
    await wrapper.get('[data-testid="memory-read-end-address"]').setValue('0x80000020')
    await wrapper.get('[data-testid="memory-read-confirm"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    expect(wrapper.get('.progress-title').text()).toBe('读取进度')
    expect(wrapper.get('[data-testid="job-state"]').text()).toBe('读取完成')

    await wrapper.get('[data-testid="start-job"]').trigger('click')
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(wrapper.get('.progress-title').text()).toBe('烧录总进度')
    expect(wrapper.get('[data-testid="job-state"]').text()).not.toBe('读取完成')

    FakeEventSource.instances[0].emit('progress', {
      job_id: 'job-1', sequence: 3, timestamp: 2, event: 'progress',
      message: '[PROGRAM] 16 / 32 Bytes (50%)', state: 'programming', progress: 0.5,
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-testid="total-progress"]').attributes('value')).toBe('0.5')
    expect(wrapper.get('[data-testid="log-viewport"]').text()).toContain('[PROGRAM] 16 / 32 Bytes (50%)')

    FakeEventSource.instances[0].emit('state', {
      job_id: 'job-1', sequence: 4, timestamp: 3, event: 'state',
      message: '', state: 'succeeded', progress: 1,
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.progress-title').text()).toBe('烧录总进度')
    expect(wrapper.get('[data-testid="job-state"]').text()).toBe('烧录完成')
    expect(wrapper.get('[data-testid="total-progress-label"]').text()).toBe('100%')
    wrapper.unmount()
  })

  it('distinguishes builtin, local Pack, and optional online target sources', async () => {
    vi.stubGlobal('fetch', viewFetch([
      { ...installedTarget, part_number: 'BUILTIN', source: 'bundle' },
      { ...installedTarget, part_number: 'LOCAL', source: 'index' },
      { ...installedTarget, part_number: 'ONLINE', installed: false, source: 'index' },
    ]))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-BUILTIN"]').exists()).toBe(true))

    expect(wrapper.get('[data-testid="target-BUILTIN"]').text()).toContain('内置可用')
    expect(wrapper.get('[data-testid="target-LOCAL"]').text()).toContain('本地 Pack')
    expect(wrapper.get('[data-testid="target-ONLINE"]').text()).toContain('可导入或联网下载')
    expect(wrapper.text()).toContain('联网更新')
    wrapper.unmount()
  })

  it('imports a local Pack from the target panel and refreshes the catalog', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="pack-import-input"]').exists()).toBe(true))

    await choosePack(wrapper)
    await vi.waitFor(() => expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith('/packs/import')),
    ).toBe(true))

    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/targets?')).length).toBeGreaterThan(1)
    expect(wrapper.text()).toContain('已导入 Vendor.Device_DFP@1.0.0')
    wrapper.unmount()
  })

  beforeEach(() => {
    FakeEventSource.instances = []
    const storage = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
      clear: () => storage.clear(),
    })
    vi.stubGlobal('fetch', viewFetch())
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.stubGlobal('confirm', vi.fn(() => true))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.doUnmock('@tauri-apps/plugin-dialog')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('adds and displays a target-scoped custom FLM', async () => {
    vi.stubGlobal('fetch', viewFetch([regularTarget]))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')

    await chooseCustomFlm(wrapper)

    await vi.waitFor(() => expect(wrapper.find('[data-testid="custom-flm-external-1"]').exists()).toBe(true))
    expect(wrapper.text()).toContain('external.flm')
    expect(wrapper.text()).toContain('0x90000000')
    const upload = vi.mocked(fetch).mock.calls.find(([url, options]) => (
      String(url).endsWith('/algorithms') && options?.method === 'POST'
    ))
    expect(upload?.[1]?.body).toBeInstanceOf(FormData)
    wrapper.unmount()
  })

  it('uses an HPM board and starts ROM programming without sector geometry', async () => {
    const fallback = viewFetch([hpmTarget])
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input).endsWith('/images/inspect')) {
        return new Response(JSON.stringify({
          image_id: 'hpm-image', file_name: 'firmware.bin', format: 'bin', size: 32,
          sha256: 'abc123', start: 0x80000400, end: 0x80000420,
          segments: [{ start: 0x80000400, end: 0x80000420 }], base_address: 0x80000400,
          sector_operations_available: false, sectors: [],
        }), { status: 200 })
      }
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-HPM5300"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-HPM5300"]').trigger('click')

    expect(wrapper.get('[data-testid="hpm-board"]').element).toHaveProperty('value', 'hpm5300evk')
    expect(wrapper.text()).toContain('内置 ROM API')
    await chooseFirmware(wrapper)
    expect(wrapper.get<HTMLInputElement>('[data-testid="bin-address-dialog-input"]').element.value).toBe('0x80000400')
    await wrapper.get('[data-testid="confirm-bin-address"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    await wrapper.get('[data-testid="start-job"]').trigger('click')
    await vi.waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith('/jobs'))).toBe(true))
    const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/jobs'))
    const body = JSON.parse(String(call?.[1]?.body))
    expect(body.board).toBe('hpm5300evk')
    expect(body.sector_addresses).toEqual([])
    wrapper.unmount()
  })

  it('keeps retrying probe discovery on page entry until MKLink appears', async () => {
    vi.useFakeTimers()
    const fallback = viewFetch()
    let probeCalls = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input).endsWith('/probes')) {
        probeCalls += 1
        return Promise.resolve(new Response(JSON.stringify(probeCalls < 4 ? [] : [probeFixture]), { status: 200 }))
      }
      return fallback(input, options)
    }))

    const wrapper = mount(await onlineFlashView())
    await flushPromises()
    expect(probeCalls).toBe(1)

    await vi.advanceTimersByTimeAsync(499)
    expect(probeCalls).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(probeCalls).toBe(2)

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(probeCalls).toBe(4)
    expect(wrapper.get('[data-testid="probe-select"]').element).toHaveProperty('value', probeFixture.unique_id)
    wrapper.unmount()
  })

  it('retries transient probe enumeration errors during backend cold start', async () => {
    vi.useFakeTimers()
    const fallback = viewFetch()
    let probeCalls = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input).endsWith('/probes')) {
        probeCalls += 1
        if (probeCalls < 3) {
          return Promise.resolve(new Response(JSON.stringify({ detail: 'backend warming up' }), { status: 500 }))
        }
        return Promise.resolve(new Response(JSON.stringify([probeFixture]), { status: 200 }))
      }
      return fallback(input, options)
    }))

    const wrapper = mount(await onlineFlashView())
    await flushPromises()
    expect(probeCalls).toBe(1)

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(probeCalls).toBe(3)
    expect(wrapper.get('[data-testid="probe-select"]').element).toHaveProperty('value', probeFixture.unique_id)
    expect(wrapper.text()).not.toContain('backend warming up')
    wrapper.unmount()
  })

  it('automatically inspects a selected BIN or HEX file without an inspect button', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')

    await chooseFirmware(wrapper, 'firmware.hex')

    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/images/inspect'))).toBe(true))
    expect(wrapper.find('[data-testid="inspect-image"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('已自动检查')
    wrapper.unmount()
  })

  it('clears a user-selected HEX file and its preview from the shared data window', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')

    await chooseFirmware(wrapper, 'firmware.hex')
    await vi.waitFor(() => expect(wrapper.text()).toContain('已自动检查'))

    const clear = wrapper.get('[data-testid="memory-read-clear"]')
    expect(clear.attributes('disabled')).toBeUndefined()
    await clear.trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).not.toContain('firmware.hex')
    expect(wrapper.text()).not.toContain('已自动检查')
    expect(wrapper.find('.metadata').exists()).toBe(false)
    expect(wrapper.get('[data-testid="memory-read-clear"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('reinspects a desktop firmware path when the same HEX file is selected again', async () => {
    const fallback = viewFetch()
    const firmwarePath = 'C:\\firmware\\firmware.hex'
    const open = vi.fn(async () => firmwarePath)
    vi.stubGlobal('isTauri', true)
    vi.doMock('@tauri-apps/plugin-dialog', () => ({ open }))
    const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/images/inspect-path')) {
        return new Response(JSON.stringify({
          image_id: `image-${open.mock.calls.length}`,
          file_name: 'firmware.hex', format: 'hex', size: 32,
          sha256: 'abc123', start: 0x08000000, end: 0x08000020,
          segments: [{ start: 0x08000000, end: 0x08000020 }], base_address: null,
          sector_operations_available: true,
          sectors: [{ address: 0x08000000, size: 0x1000 }],
        }), { status: 200 })
      }
      if (url.includes('/images/source-status?')) {
        return new Response(JSON.stringify({
          available: true, file_name: 'firmware.hex', size: 32, mtime_ns: 100,
        }), { status: 200 })
      }
      return fallback(input, options)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')

    await wrapper.get('[data-testid="firmware-trigger"]').trigger('click')
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/images/inspect-path'))).toHaveLength(1))
    expect(wrapper.text()).toContain('已自动检查')

    await wrapper.get('[data-testid="firmware-trigger"]').trigger('click')
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/images/inspect-path'))).toHaveLength(2))
    expect(wrapper.text()).toContain('已自动检查')
    expect(open).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('automatically reloads a rebuilt browser firmware file from its retained handle', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    let currentFile = new File(['old'], 'firmware.hex', { lastModified: 100 })
    const handle = {
      kind: 'file' as const,
      name: currentFile.name,
      getFile: vi.fn(async () => currentFile),
    }
    vi.stubGlobal('showOpenFilePicker', vi.fn(async () => [handle]))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')

    await wrapper.get('[data-testid="firmware-trigger"]').trigger('click')
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/images/inspect'))).toHaveLength(1))
    currentFile = new File(['rebuilt-firmware'], 'firmware.hex', { lastModified: 200 })

    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/images/inspect'))).toHaveLength(2), { timeout: 3000 })
    const inspectCalls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/images/inspect'))
    const firstFile = (inspectCalls[0][1]?.body as FormData).get('file') as File
    const rebuiltFile = (inspectCalls[1][1]?.body as FormData).get('file') as File
    expect(firstFile.size).toBe(3)
    expect(rebuiltFile.size).toBe(16)
    expect(wrapper.text()).toContain('已自动加载重新编译的 firmware.hex')
    wrapper.unmount()
  })

  it('waits for an explicit BIN base and inspects automatically after it is entered', async () => {
    const fetchMock = viewFetch()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')

    await chooseFirmware(wrapper, 'firmware.bin')
    await new Promise(resolve => setTimeout(resolve, 250))

    expect(wrapper.get('[data-testid="bin-address-dialog"]').attributes('role')).toBeUndefined()
    expect(wrapper.get('[data-testid="bin-address-dialog"] [role="dialog"]').attributes('aria-modal')).toBe('true')
    expect(wrapper.get('[data-testid="base-error"]').text()).toContain('BIN 基地址')
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/images/inspect'))).toBe(false)

    await wrapper.get('[data-testid="bin-address-dialog-input"]').setValue('0x80000000')
    await wrapper.get('[data-testid="confirm-bin-address"]').trigger('click')

    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/images/inspect'))).toBe(true))
    expect(wrapper.find('[data-testid="bin-address-dialog"]').exists()).toBe(false)
    expect(wrapper.get<HTMLInputElement>('[data-testid="bin-base"]').element.value).toBe('0x80000000')
    expect(wrapper.text()).toContain('已自动检查')
    wrapper.unmount()
  })

  it('uses the HPM XIP base when an HPM target is selected', async () => {
    const fetchMock = viewFetch([hpmTarget])
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-HPM5300"]').exists()).toBe(true))

    await wrapper.get('[data-testid="target-HPM5300"]').trigger('click')
    await chooseFirmware(wrapper)

    expect(wrapper.get<HTMLInputElement>('[data-testid="bin-address-dialog-input"]').element.value).toBe('0x80000400')
    await wrapper.get('[data-testid="confirm-bin-address"]').trigger('click')
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/images/inspect'))).toBe(true))
    expect(wrapper.get<HTMLInputElement>('[data-testid="bin-base"]').element.value).toBe('0x80000400')
    wrapper.unmount()
  })

  it('does not carry a BIN base across different targets', async () => {
    const fetchMock = viewFetch([regularTarget, hpmTarget])
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))

    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-address-dialog-input"]').setValue('0x08000400')
    await wrapper.get('[data-testid="confirm-bin-address"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="firmware-drop-zone"]').text()).toContain('固件检查失败'))
    await wrapper.get('[data-testid="target-HPM5300"]').trigger('click')

    expect(wrapper.get<HTMLInputElement>('[data-testid="bin-base"]').element.value).toBe('0x80000400')
    wrapper.unmount()
  })

  it('prompts for a BIN base when firmware is dropped into the workspace', async () => {
    const wrapper = mount(await onlineFlashView())
    const file = new File(['firmware'], 'dropped.bin')

    await wrapper.get('[data-testid="firmware-drop-zone"]').trigger('drop', {
      dataTransfer: { files: [file] },
    })

    expect(wrapper.get('[data-testid="bin-address-dialog"]').text()).toContain('dropped.bin')
    expect(wrapper.get('[data-testid="confirm-bin-address"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('rejects an invalid BIN base and keeps start disabled until server inspection succeeds', async () => {
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('80000000')

    expect(wrapper.get('[data-testid="base-error"]').text()).toContain('0x')
    expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    wrapper.unmount()
  })

  it('aborts and ignores an in-flight inspection when the BIN base changes', async () => {
    const fallback = viewFetch()
    let inspectionSignal: AbortSignal | null = null
    let resolveInspection!: (response: Response) => void
    const pendingInspection = new Promise<Response>(resolve => { resolveInspection = resolve })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input).endsWith('/images/inspect')) {
        inspectionSignal = options?.signal ?? null
        return pendingInspection
      }
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(inspectionSignal).not.toBeNull())

    await wrapper.get('[data-testid="bin-base"]').setValue('0x80001000')
    expect(inspectionSignal?.aborted).toBe(true)
    resolveInspection(new Response(JSON.stringify({
      image_id: 'stale-image', file_name: 'firmware.bin', format: 'bin', size: 32,
      sha256: 'stale', start: 0x80000000, end: 0x80000020,
      segments: [{ start: 0x80000000, end: 0x80000020 }], base_address: 0x80000000,
    }), { status: 200 }))
    await Promise.resolve()
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).not.toContain('stale-image')
    wrapper.unmount()
  })

  it('aborts a deferred inspection before unmount cleanup can start preview work', async () => {
    const fallback = viewFetch()
    let inspectionSignal: AbortSignal | null = null
    let resolveInspection!: (response: Response) => void
    const pending = new Promise<Response>(resolve => { resolveInspection = resolve })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input).endsWith('/images/inspect')) { inspectionSignal = options?.signal ?? null; return pending }
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(inspectionSignal).not.toBeNull())

    wrapper.unmount()
    expect(inspectionSignal?.aborted).toBe(true)
    resolveInspection(new Response(JSON.stringify({
      image_id: 'late', file_name: 'firmware.bin', format: 'bin', size: 32, sha256: 'late',
      start: 0x80000000, end: 0x80000020, segments: [], base_address: 0x80000000,
    }), { status: 200 }))
    for (let index = 0; index < 20; index += 1) await Promise.resolve()
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/preview?'))).toHaveLength(0)
  })

  it('does not trust an install response when refreshed exact target remains uninstalled', async () => {
    const missing = { ...regularTarget, installed: false, source: 'index' }
    vi.stubGlobal('fetch', viewFetch([missing]))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))

    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('安装后索引仍未确认'))

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('下载'))
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith('/packs/install'))).toBe(true)
    expect(wrapper.get('[data-testid="pack-status"]').text()).toContain('未就绪')
    expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('debounces target searches without relying on real-time sleeps', async () => {
    vi.useFakeTimers()
    const wrapper = mount(await onlineFlashView())
    const initialSearchCount = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/targets?')).length

    await wrapper.get('input[aria-label="搜索器件"]').setValue('HPM 53')
    await vi.advanceTimersByTimeAsync(299)
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('q=HPM+53'))).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1)

    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('q=HPM+53'))).toHaveLength(1)
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/targets?')).length).toBe(initialSearchCount + 1)
    wrapper.unmount()
  })

  it('commits only the latest target search response', async () => {
    vi.useFakeTimers()
    const fallback = viewFetch()
    let resolveInitial!: (response: Response) => void
    let initialSignal: AbortSignal | null = null
    const initial = new Promise<Response>(resolve => { resolveInitial = resolve })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      if (url.includes('/targets?q=')) {
        const query = new URL(url, 'http://local').searchParams.get('q')
        if (!query) { initialSignal = options?.signal ?? null; return initial }
        return Promise.resolve(new Response(JSON.stringify([{ ...installedTarget, part_number: 'NEW-TARGET' }]), { status: 200 }))
      }
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await wrapper.get('input[aria-label="搜索器件"]').setValue('new')
    await vi.advanceTimersByTimeAsync(300)
    await vi.waitFor(() => expect(wrapper.text()).toContain('NEW-TARGET'))
    expect(initialSignal?.aborted).toBe(true)
    resolveInitial(new Response(JSON.stringify([{ ...installedTarget, part_number: 'OLD-TARGET' }]), { status: 200 }))
    for (let index = 0; index < 10; index += 1) await Promise.resolve()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('NEW-TARGET')
    expect(wrapper.text()).not.toContain('OLD-TARGET')
    wrapper.unmount()
  })

  it('locks Pack install and cancel operations against duplicate clicks', async () => {
    const missing = { ...regularTarget, installed: false, source: 'index' }
    const fallback = viewFetch([missing])
    let resolveInstall!: (response: Response) => void
    let resolveCancel!: (response: Response) => void
    const install = new Promise<Response>(resolve => { resolveInstall = resolve })
    const cancel = new Promise<Response>(resolve => { resolveCancel = resolve })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/packs/install')) return install
      if (url.endsWith('/packs/cancel')) return cancel
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    const target = wrapper.get('[data-testid="target-DEVICE_A"]')
    await target.trigger('click'); await target.trigger('click')
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith('/packs/install'))).toHaveLength(1)
    expect(target.attributes('disabled')).toBeDefined()
    const cancelButton = wrapper.get('[data-testid="pack-cancel"]')
    await cancelButton.trigger('click'); await cancelButton.trigger('click')
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith('/packs/cancel'))).toHaveLength(1)
    expect(cancelButton.attributes('disabled')).toBeDefined()
    resolveCancel(new Response(JSON.stringify({ status: 'cancelled' }), { status: 200 }))
    for (let index = 0; index < 5; index += 1) await Promise.resolve()
    expect(target.attributes('disabled')).toBeDefined()
    expect(cancelButton.attributes('disabled')).toBeDefined()
    resolveInstall(new Response(JSON.stringify({ result: { status: 'installed', part_number: 'DEVICE_A' }, events: [] }), { status: 200 }))
    wrapper.unmount()
  })

  it('replays from sequence zero, deduplicates logs, and explicitly reconnects after a stream error', async () => {
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    await wrapper.get('[data-testid="start-job"]').trigger('click')
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const source = FakeEventSource.instances[0]
    const log = { job_id: 'job-1', sequence: 1, timestamp: 1, event: 'log', message: 'programming', state: null, progress: null }

    expect(source.url).toContain('after=0')
    source.emit('log', log)
    source.emit('log', log)
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('[data-testid="log-line"]').filter(line => line.text().includes('programming'))).toHaveLength(1)

    source.emitNativeError()
    await wrapper.vm.$nextTick()
    await wrapper.get('[data-testid="reconnect-stream"]').trigger('click')
    expect(FakeEventSource.instances[1].url).toContain('after=1')
    wrapper.unmount()
  })

  it('makes a server-named SSE error reconnectable from the last sequence', async () => {
    const wrapper = mount(await onlineFlashView())
    await readyAndStart(wrapper)
    const source = FakeEventSource.instances[0]
    source.emit('log', { job_id: 'job-1', sequence: 7, timestamp: 1, event: 'log', message: 'checkpoint', state: null, progress: null })
    source.emit('error', { code: 'BACKEND_LOST', message: 'worker stream ended' })
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('BACKEND_LOST')
    await wrapper.get('[data-testid="reconnect-stream"]').trigger('click')
    expect(FakeEventSource.instances[1].url).toContain('after=7')
    wrapper.unmount()
  })

  it('shows the terminal backend error before the job stream closes', async () => {
    const wrapper = mount(await onlineFlashView())
    await readyAndStart(wrapper)
    const source = FakeEventSource.instances[0]

    source.emit('error', {
      job_id: 'job-1', sequence: 6, timestamp: 1, event: 'error',
      message: 'Failed to connect to MKLink on COM48', state: 'failed', progress: null,
    })
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="job-state"]').text()).toContain('烧录失败')
    expect(wrapper.text()).toContain('Failed to connect to MKLink on COM48')
    expect(source.closed).toBe(true)
    wrapper.unmount()
  })

  it('keeps the newest viewport rows when preview requests resolve out of order', async () => {
    const fallback = viewFetch()
    let resolveOld!: (response: Response) => void
    let resolveNew!: (response: Response) => void
    const oldPage = new Promise<Response>(resolve => { resolveOld = resolve })
    const newPage = new Promise<Response>(resolve => { resolveNew = resolve })
    const previewResponse = (character: string, address: number) => new Response(JSON.stringify({
      address, length: 4096, data_base64: btoa(character.repeat(4096)), present: Array(4096).fill(true),
    }), { status: 200 })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/images/inspect')) return Promise.resolve(new Response(JSON.stringify({
        image_id: 'large-image', file_name: 'firmware.bin', format: 'bin', size: 16384,
        sha256: 'large', start: 0x80000000, end: 0x80004000,
        segments: [{ start: 0x80000000, end: 0x80004000 }], base_address: 0x80000000,
      }), { status: 200 }))
      if (url.includes('/preview?')) {
        const offset = Number(new URL(url, 'http://local').searchParams.get('offset'))
        if (offset === 0) return Promise.resolve(previewResponse('A', 0x80000000))
        if (offset === 4096) return oldPage
        if (offset === 8192) return newPage
      }
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(wrapper.text()).toContain('AAAAAAAA'))
    const scroller = wrapper.get('.hex-scroll')
    Object.defineProperty(scroller.element, 'clientHeight', { configurable: true, value: 200 })
    Object.defineProperty(scroller.element, 'scrollTop', { configurable: true, writable: true, value: 6000 })
    await scroller.trigger('scroll')
    ;(scroller.element as HTMLElement).scrollTop = 12000
    await scroller.trigger('scroll')

    resolveNew(previewResponse('N', 0x80002000))
    await vi.waitFor(() => expect(wrapper.text()).toContain('NNNNNNNN'))
    resolveOld(previewResponse('O', 0x80001000))
    for (let index = 0; index < 100; index += 1) await Promise.resolve()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('NNNNNNNN')
    expect(wrapper.text()).not.toContain('OOOOOOOO')
    wrapper.unmount()
  })

  it('submits canonical actions and keeps connect/disconnect mandatory', async () => {
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    const choices = wrapper.findAll('.action-choices label')
    expect(choices[0].get('input').attributes('disabled')).toBeDefined()
    expect(choices.at(-1)?.get('input').attributes('disabled')).toBeDefined()
    await choices[1].get('input').setValue(false)
    await choices[3].get('input').setValue(false)
    await choices[1].get('input').setValue(true)
    await choices[3].get('input').setValue(true)
    await wrapper.get('[data-testid="start-job"]').trigger('click')
    await vi.waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith('/jobs'))).toBe(true))
    const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/jobs'))

    expect(JSON.parse(String(call?.[1]?.body)).actions).toEqual(['connect', 'erase', 'program', 'verify', 'reset', 'disconnect'])
    expect(JSON.parse(String(call?.[1]?.body)).sector_addresses).toEqual([0x80000000])
    wrapper.unmount()
  })

  it('blocks erase-based programming when the FLM sector table is unavailable', async () => {
    const fallback = viewFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input).endsWith('/images/inspect')) {
        return new Response(JSON.stringify({
          image_id: 'image-1', file_name: 'firmware.bin', format: 'bin', size: 32,
          sha256: 'abc123', start: 0x80000000, end: 0x80000020,
          segments: [{ start: 0x80000000, end: 0x80000020 }], base_address: 0x80000000,
          sector_operations_available: false, sectors: [],
        }), { status: 200 })
      }
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(wrapper.text()).toContain('32 bytes'))

    expect(wrapper.text()).toContain('扇区几何信息不可验证')
    expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeDefined()
    await wrapper.findAll('.action-choices label')[1].get('input').setValue(false)
    expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('does not let a late stop response overwrite an SSE success terminal', async () => {
    const fallback = viewFetch()
    let resolveStop!: (response: Response) => void
    const pendingStop = new Promise<Response>(resolve => { resolveStop = resolve })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => (
      String(input).endsWith('/jobs/job-1/stop') ? pendingStop : fallback(input, options)
    )))
    const wrapper = mount(await onlineFlashView())
    await readyAndStart(wrapper)
    await wrapper.get('[data-testid="stop-job"]').trigger('click')
    FakeEventSource.instances[0].emit('state', {
      job_id: 'job-1', sequence: 9, timestamp: 2, event: 'state', message: '', state: 'succeeded', progress: 1,
    })
    await vi.waitFor(() => expect(wrapper.get('[data-testid="job-state"]').text()).toContain('烧录完成'))
    resolveStop(new Response(JSON.stringify({ state: 'stopped', job_id: 'job-1' }), { status: 200 }))
    for (let index = 0; index < 10; index += 1) await Promise.resolve()
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="job-state"]').text()).toContain('烧录完成')
    expect(wrapper.find('.waiting').exists()).toBe(false)
    wrapper.unmount()
  })

  it('restores the previous job state after a failed stop so stop can be retried', async () => {
    const fallback = viewFetch()
    let stopAttempts = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input).endsWith('/jobs/job-1/stop')) {
        stopAttempts += 1
        return Promise.resolve(new Response(JSON.stringify({ detail: 'stop failed' }), { status: 500, statusText: 'fail' }))
      }
      return fallback(input, options)
    }))
    const wrapper = mount(await onlineFlashView())
    await readyAndStart(wrapper)
    await wrapper.get('[data-testid="stop-job"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="job-state"]').text()).toContain('QUEUED'))
    expect(wrapper.get('[data-testid="stop-job"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-testid="stop-job"]').trigger('click')
    await vi.waitFor(() => expect(stopAttempts).toBe(2))
    wrapper.unmount()
  })

  it('uses a synchronous creating-job latch for normal and chip-erase starts', async () => {
    const fallback = viewFetch()
    let resolveJob!: (response: Response) => void
    const pendingJob = new Promise<Response>(resolve => { resolveJob = resolve })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => (
      String(input).endsWith('/jobs') && options?.method === 'POST' ? pendingJob : fallback(input, options)
    )))
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    const start = wrapper.get('[data-testid="start-job"]')
    await start.trigger('click'); await start.trigger('click')
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith('/jobs'))).toHaveLength(1)
    expect(start.attributes('disabled')).toBeDefined()
    resolveJob(new Response(JSON.stringify({ job_id: 'job-1', job: { state: 'queued' } }), { status: 200 }))
    wrapper.unmount()

    let resolveErase!: (response: Response) => void
    const pendingErase = new Promise<Response>(resolve => { resolveErase = resolve })
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => (
      String(input).endsWith('/jobs') && options?.method === 'POST' ? pendingErase : fallback(input, options)
    )))
    FakeEventSource.instances = []
    const eraseWrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(eraseWrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await eraseWrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    const chipErase = eraseWrapper.get('[data-testid="chip-erase"]')
    await chipErase.trigger('click'); await chipErase.trigger('click')
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith('/jobs'))).toHaveLength(1)
    resolveErase(new Response(JSON.stringify({ job_id: 'erase', job: { state: 'queued' } }), { status: 200 }))
    eraseWrapper.unmount()
  })

  it('survives localStorage quota errors and reports a non-sensitive warning', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => { throw new DOMException('quota details', 'QuotaExceededError') },
    })
    const wrapper = mount(await onlineFlashView())
    await wrapper.get('[data-testid="frequency"]').setValue('4000000')
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('本地设置未保存')
    expect(wrapper.text()).not.toContain('quota details')
    expect(wrapper.get('[data-testid="frequency"]').element).toHaveProperty('value', '4000000')
    wrapper.unmount()
  })

  it('shows one total job progress indicator', async () => {
    const wrapper = mount(await onlineFlashView())
    await readyAndStart(wrapper)
    FakeEventSource.instances[0].emit('progress', {
      job_id: 'job-1', sequence: 3, timestamp: 2, event: 'progress', message: '', state: 'programming', progress: 0.4,
    })
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="total-progress"]').attributes('value')).toBe('0.4')
    expect(wrapper.get('[data-testid="total-progress-label"]').text()).toBe('40%')
    expect(wrapper.find('[data-testid="stage-progress"]').exists()).toBe(false)
    expect(wrapper.findAll('progress')).toHaveLength(1)
    wrapper.unmount()
  })

  it('shows STOPPING and waits for a terminal event after stop', async () => {
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await chooseFirmware(wrapper)
    await wrapper.get('[data-testid="bin-base"]').setValue('0x80000000')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="start-job"]').attributes('disabled')).toBeUndefined())
    await wrapper.get('[data-testid="start-job"]').trigger('click')
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    await wrapper.get('[data-testid="stop-job"]').trigger('click')
    expect(wrapper.get('[data-testid="job-state"]').text()).toContain('STOPPING')
    expect(wrapper.text()).toContain('等待探针安全停止')
    expect(wrapper.get('[data-testid="stop-job"]').attributes('disabled')).toBeDefined()
    expect(FakeEventSource.instances[0].closed).toBe(false)

    FakeEventSource.instances[0].emit('state', {
      job_id: 'job-1', sequence: 2, timestamp: 2, event: 'state', message: '', state: 'stopped', progress: 1,
    })
    await vi.waitFor(() => expect(wrapper.get('[data-testid="job-state"]').text()).toContain('已停止'))
    wrapper.unmount()
  })

  it('persists settings but never File data or an opaque image snapshot', async () => {
    const wrapper = mount(await onlineFlashView())
    await wrapper.get('[data-testid="frequency"]').setValue('4000000')
    await chooseFirmware(wrapper)
    const stored = localStorage.getItem('mklink.onlineFlash.settings') ?? ''

    expect(stored).toContain('4000000')
    expect(stored).not.toContain('firmware')
    expect(stored).not.toContain('image_id')
    wrapper.unmount()
  })

  it('requires explicit confirmation for chip erase and keeps sectors disabled without reliable geometry', async () => {
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="target-DEVICE_A"]').exists()).toBe(true))
    await wrapper.get('[data-testid="target-DEVICE_A"]').trigger('click')
    await wrapper.get('[data-testid="chip-erase"]').trigger('click')

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('全片擦除'))
    expect(wrapper.get('[data-testid="select-all-sectors"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="range-erase"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('加载固件后显示扇区表')
    expect(wrapper.text()).not.toContain('扇区几何信息不可验证')
    wrapper.unmount()
  })
  it('exposes stable packaged-HIL selectors', async () => {
    const wrapper = mount(await onlineFlashView())
    await vi.waitFor(() => expect(wrapper.find('[data-testid="probe-select"]').exists()).toBe(true))

    expect(wrapper.find('[data-testid="target-search"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pack-update-index"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('online flash component quality', () => {
  it('bounds the desktop workspace to the viewport and scrolls HEX rows internally', () => {
    expect(onlineFlashViewSource).toMatch(/height:calc\(100dvh/)
    expect(onlineFlashViewSource).toMatch(/min-height:0/)
    expect(firmwareWorkspaceSource).toMatch(/\.hex-scroll\{min-height:0;height:auto;flex:1/)
    expect(onlineFlashViewSource).toContain('@progress="onMemoryReadProgress"')
    expect(onlineFlashViewSource).toContain('@log="onMemoryReadLog"')
    expect(onlineFlashViewSource).toContain(':total-progress="progressValue"')
  })

  function mockLogGeometry(viewport: ReturnType<typeof mount>['element'], values: { top: number; height: number; total: number }) {
    Object.defineProperty(viewport, 'scrollTop', {
      configurable: true,
      get: () => values.top,
      set: value => { values.top = Number(value) },
    })
    Object.defineProperty(viewport, 'clientHeight', { configurable: true, get: () => values.height })
    Object.defineProperty(viewport, 'scrollHeight', { configurable: true, get: () => values.total })
  }

  it('virtualizes 5000 log lines and can scroll from early to middle history', async () => {
    const lines = Array.from({ length: 5000 }, (_, index) => `line-${index}`)
    const wrapper = mount(FlashLogPanel, { props: { lines, streamDisconnected: false } })
    const viewport = wrapper.get('[data-testid="log-viewport"]')
    const metrics = { top: 0, height: 135, total: lines.length * 18 }
    mockLogGeometry(viewport.element, metrics)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('[data-testid="log-line"]').length).toBeLessThan(40)
    expect(wrapper.text()).toContain('line-4999')

    metrics.top = 0
    await viewport.trigger('scroll')
    expect(wrapper.text()).toContain('line-0')
    metrics.top = 2500 * 18
    await viewport.trigger('scroll')
    expect(wrapper.text()).toContain('line-2500')
    expect(wrapper.findAll('[data-testid="log-line"]').length).toBeLessThan(40)
  })

  it('keeps long virtual log rows at the fixed 18px height with accessible full text', () => {
    const longLine = `programming ${'0123456789'.repeat(30)}`
    const wrapper = mount(FlashLogPanel, { props: { lines: [longLine], streamDisconnected: false } })
    const row = wrapper.get('[data-testid="log-line"]')

    expect(row.classes()).toContain('log-line')
    expect(row.attributes('title')).toBe(longLine)
    expect(row.attributes('aria-label')).toBe(longLine)
    expect(logPanelSource).toContain('.log-line{height:18px;line-height:18px;box-sizing:border-box;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}')
    expect(wrapper.findAll('[data-testid="log-line"]')).toHaveLength(1)
  })

  it('follows the latest row when logs first arrive and when already near the bottom', async () => {
    const wrapper = mount(FlashLogPanel, { props: { lines: [], streamDisconnected: false } })
    const viewport = wrapper.get('[data-testid="log-viewport"]')
    const metrics = { top: 0, height: 54, total: 100 * 18 }
    mockLogGeometry(viewport.element, metrics)

    const lines = Array.from({ length: 100 }, (_, index) => `line-${index}`)
    await wrapper.setProps({ lines })
    await wrapper.vm.$nextTick()
    expect(metrics.top).toBe(metrics.total - metrics.height)
    expect(wrapper.text()).toContain('line-99')

    metrics.top = metrics.total - metrics.height - 18
    await viewport.trigger('scroll')
    metrics.total += 18
    await wrapper.setProps({ lines: [...lines, 'line-100'] })
    await wrapper.vm.$nextTick()
    expect(metrics.top).toBe(metrics.total - metrics.height)
    expect(wrapper.text()).toContain('line-100')
  })

  it('preserves an upstream position on append and exposes a jump-to-latest recovery', async () => {
    const lines = Array.from({ length: 100 }, (_, index) => `line-${index}`)
    const wrapper = mount(FlashLogPanel, { props: { lines, streamDisconnected: false } })
    const viewport = wrapper.get('[data-testid="log-viewport"]')
    const metrics = { top: 40 * 18, height: 54, total: 100 * 18 }
    mockLogGeometry(viewport.element, metrics)
    await viewport.trigger('scroll')

    metrics.total += 18
    await wrapper.setProps({ lines: [...lines, 'line-100'] })
    await wrapper.vm.$nextTick()
    expect(metrics.top).toBe(40 * 18)
    expect(wrapper.get('[data-testid="jump-latest"]').isVisible()).toBe(true)

    await wrapper.get('[data-testid="jump-latest"]').trigger('click')
    await wrapper.vm.$nextTick()
    expect(metrics.top).toBe(metrics.total - metrics.height)
    expect(wrapper.find('[data-testid="jump-latest"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('line-100')
  })

  it('opens the visually hidden file input from a keyboard-focusable trigger', async () => {
    const wrapper = mount(FirmwareWorkspace, { props: {
      file: null, baseAddress: '', baseError: '', inspection: null, rows: [],
      paddingTop: 0, paddingBottom: 0, loading: false, error: '',
    } })
    const input = wrapper.get('[data-testid="firmware-input"]')
    const click = vi.spyOn(input.element as HTMLInputElement, 'click')
    const trigger = wrapper.get('[data-testid="firmware-trigger"]')
    expect(trigger.attributes('tabindex')).toBe('0')
    expect(input.classes()).toContain('visually-hidden')
    await trigger.trigger('keydown', { key: 'Enter' })
    expect(click).toHaveBeenCalledTimes(1)
  })

  it('clears the fallback browser input so selecting the same path emits again', async () => {
    const wrapper = mount(FirmwareWorkspace, { props: {
      file: null, baseAddress: '', baseError: '', inspection: null, rows: [],
      paddingTop: 0, paddingBottom: 0, loading: false, error: '',
    } })
    const input = wrapper.get('[data-testid="firmware-input"]')
    const file = new File(['firmware'], 'demo.bin')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })

    await input.trigger('change')
    await input.trigger('change')

    expect((input.element as HTMLInputElement).value).toBe('')
    expect(wrapper.emitted('file')).toEqual([[file], [file]])
  })

  it('accepts firmware dropped into the online workspace', async () => {
    const wrapper = mount(FirmwareWorkspace, { props: {
      file: null, sourcePath: '', baseAddress: '', baseError: '', inspection: null, rows: [],
      paddingTop: 0, paddingBottom: 0, loading: false, error: '',
    } })
    const file = new File(['hex'], 'dropped.hex')

    await wrapper.get('[data-testid="firmware-drop-zone"]').trigger('drop', {
      dataTransfer: { files: [file] },
    })

    expect(wrapper.emitted('dropFiles')?.[0]).toEqual([[file]])
  })

  it('shows read data in the HEX window and emits save and clear actions', async () => {
    const wrapper = mount(FirmwareWorkspace, { props: {
      file: null, baseAddress: '', baseError: '', inspection: null, rows: [],
      paddingTop: 0, paddingBottom: 0, loading: false, error: '',
      memoryData: new Uint8Array([0x41, 0x42, 0x43]), memoryAddress: 0x1000,
    } })

    expect(wrapper.get('[data-testid="memory-read-submit"] .lucide-upload').exists()).toBe(true)
    expect(wrapper.get('.metadata').text()).toContain('0x00001003')
    expect(wrapper.get('.hex-row').text()).toContain('414243')
    await wrapper.get('[data-testid="memory-read-save"]').trigger('click')
    await wrapper.get('[data-testid="memory-read-clear"]').trigger('click')
    expect(wrapper.emitted('save')).toHaveLength(1)
    expect(wrapper.emitted('clearData')).toHaveLength(1)
  })

  it('wraps the action bar controls for narrow layouts', () => {
    expect(actionBarSource).toContain('flex-wrap:wrap')
    expect(actionBarSource).toContain('max-width:100%')
  })

  it('keeps the BIN base-address label and input on one stable line', () => {
    expect(firmwareWorkspaceSource).toMatch(/\.base-field\{[^}]*flex:0 0 auto[^}]*white-space:nowrap/s)
    expect(firmwareWorkspaceSource).toMatch(/\.base-field input\{[^}]*flex:0 0 92px[^}]*min-width:92px/s)
  })
})
