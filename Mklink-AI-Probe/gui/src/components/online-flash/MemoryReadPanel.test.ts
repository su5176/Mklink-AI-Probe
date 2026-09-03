import { flushPromises, mount } from '@vue/test-utils'
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import MemoryReadPanel from './MemoryReadPanel.vue'

const componentSource = fs.readFileSync(
  path.resolve(process.cwd(), 'src/components/online-flash/MemoryReadPanel.vue'), 'utf8',
).replaceAll('\r\n', '\n')

describe('MemoryReadPanel', () => {
  it('fills the selected target Flash range and preserves manual edits', async () => {
    const wrapper = mount(MemoryReadPanel, {
      props: {
        probeId: 'probe', targetPart: 'STM32F103RE', hpm: false, embedded: true,
        frequency: 1_000_000, connectMode: 'halt', resetMode: 'default',
        memoryRegions: [{ name: 'flash', start: 0x08000000, length: 0x80000, sector_size: 0x800 }],
      },
    })

    ;(wrapper.vm as unknown as { openReadDialog: () => void }).openReadDialog()
    await wrapper.vm.$nextTick()
    expect((wrapper.get('[data-testid="memory-read-address"]').element as HTMLInputElement).value).toBe('0x08000000')
    expect((wrapper.get('[data-testid="memory-read-end-address"]').element as HTMLInputElement).value).toBe('0x08080000')

    await wrapper.get('[data-testid="memory-read-address"]').setValue('0x08001000')
    await wrapper.setProps({
      memoryRegions: [{ name: 'flash', start: 0x08000000, length: 0x100000, sector_size: 0x800 }],
    })
    await wrapper.vm.$nextTick()
    expect((wrapper.get('[data-testid="memory-read-address"]').element as HTMLInputElement).value).toBe('0x08001000')
    expect((wrapper.get('[data-testid="memory-read-end-address"]').element as HTMLInputElement).value).toBe('0x08080000')
    wrapper.unmount()
  })

  it('renders the exposed read dialog while embedded in the online workspace', async () => {
    const wrapper = mount(MemoryReadPanel, {
      props: {
        probeId: 'probe', targetPart: 'STM32F103C8', hpm: false, embedded: true,
        frequency: 1_000_000, connectMode: 'halt', resetMode: 'default',
      },
    })

    expect(wrapper.find('[data-testid="memory-read-panel"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { openReadDialog: () => void }).openReadDialog()
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[role="dialog"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="memory-read-address"]').exists()).toBe(true)
    expect(wrapper.get('.memory-read-dialog-icon .lucide-arrow-up-from-line').exists()).toBe(true)
    expect(wrapper.get('[data-testid="memory-read-confirm"] .lucide-upload').exists()).toBe(true)
  })

  it('isolates the dialog header from global header theme rules', () => {
    expect(componentSource).toContain('.memory-read-dialog > .memory-read-dialog-header {')
    expect(componentSource).toContain('background: transparent; color: var(--memory-dialog-text);')
    expect(componentSource).toContain('padding: 0 0 14px;')
  })

  it('enables HPM reads through the binary dump path', async () => {
    const wrapper = mount(MemoryReadPanel, {
      props: {
        probeId: 'probe', targetPart: 'HPM5300', hpm: true, board: 'hpm5300evk',
        frequency: 1_000_000, connectMode: 'halt', resetMode: 'default',
        memoryRegions: [{ name: 'hpm-xpi', start: 0x80000000, length: 0x80000, sector_size: 0 }],
      },
    })
    expect(wrapper.text()).toContain('dump_memory')
    expect(wrapper.find('[data-testid="memory-read-submit"]').exists()).toBe(true)
    ;(wrapper.vm as unknown as { openReadDialog: () => void }).openReadDialog()
    await wrapper.vm.$nextTick()
    expect((wrapper.get('[data-testid="memory-read-address"]').element as HTMLInputElement).value).toBe('0x80000000')
  })

  it('uses the HPM 512 KiB XPI range when no memory map is available', async () => {
    const wrapper = mount(MemoryReadPanel, {
      props: {
        probeId: 'probe', targetPart: 'HPM5301', hpm: true, board: 'hpm5301evklite',
        frequency: 1_000_000, connectMode: 'halt', resetMode: 'default',
      },
    })
    ;(wrapper.vm as unknown as { openReadDialog: () => void }).openReadDialog()
    await wrapper.vm.$nextTick()
    expect((wrapper.get('[data-testid="memory-read-address"]').element as HTMLInputElement).value).toBe('0x80000000')
    expect((wrapper.get('[data-testid="memory-read-end-address"]').element as HTMLInputElement).value).toBe('0x80080000')
    wrapper.unmount()
  })

  it('prompts for a range, reads it in chunks, then saves the returned BIN', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(new Uint8Array([1, 2, 3, 4]), {
      status: 200,
      headers: { 'Content-Type': 'application/octet-stream' },
    }))
    vi.stubGlobal('fetch', fetch)
    const click = vi.fn()
    const remove = vi.fn()
    const createElement = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation(tag => tag === 'a'
      ? ({ href: '', download: '', style: {}, click, remove } as unknown as HTMLAnchorElement)
      : createElement(tag))
    vi.spyOn(document.body, 'appendChild').mockImplementation(node => node)
    vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:test'), revokeObjectURL: vi.fn() })

    const wrapper = mount(MemoryReadPanel, {
      props: {
        probeId: 'probe', targetPart: 'STM32F103C8', hpm: false,
        frequency: 1_000_000, connectMode: 'halt', resetMode: 'default',
      },
    })
    expect(wrapper.get('[data-testid="memory-read-submit"] .lucide-upload').exists()).toBe(true)
    await wrapper.get('[data-testid="memory-read-submit"]').trigger('click')
    await wrapper.get('[data-testid="memory-read-address"]').setValue('0x1000')
    await wrapper.get('[data-testid="memory-read-end-address"]').setValue('0x1004')
    await wrapper.get('[data-testid="memory-read-confirm"]').trigger('click')
    await flushPromises()

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/online-flash/memory/read'),
      expect.objectContaining({ method: 'POST' }),
    )
    expect(click).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="memory-read-progress"]').text()).toContain('100%')
    await wrapper.get('[data-testid="memory-read-save"]').trigger('click')
    expect(click).toHaveBeenCalledOnce()
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('uses the target sector size for each read request', async () => {
    const fetch = vi.fn().mockImplementation(async (_url: string, options: RequestInit) => {
      const size = Number((JSON.parse(String(options.body)) as { size: number }).size)
      return new Response(new Uint8Array(size), {
        status: 200,
        headers: { 'Content-Type': 'application/octet-stream' },
      })
    })
    vi.stubGlobal('fetch', fetch)

    const wrapper = mount(MemoryReadPanel, {
      props: {
        probeId: 'probe', targetPart: 'STM32F103C8', hpm: false,
        frequency: 1_000_000, connectMode: 'halt', resetMode: 'default',
        memoryRegions: [{ name: 'flash', start: 0x1000, length: 0x1000, sector_size: 0x800 }],
      },
    })
    await wrapper.get('[data-testid="memory-read-submit"]').trigger('click')
    await wrapper.get('[data-testid="memory-read-address"]').setValue('0x1000')
    await wrapper.get('[data-testid="memory-read-end-address"]').setValue('0x2000')
    expect(wrapper.text()).toContain('按目标 Flash 扇区分块（2048 字节）')
    await wrapper.get('[data-testid="memory-read-confirm"]').trigger('click')
    await flushPromises()

    expect(fetch).toHaveBeenCalledOnce()
    const payload = JSON.parse(String(fetch.mock.calls[0]?.[1].body)) as { size: number; chunk_sizes: number[] }
    expect(payload.size).toBe(0x1000)
    expect(payload.chunk_sizes).toEqual([0x800, 0x800])
    expect(wrapper.get('[data-testid="memory-read-log"]').text()).toContain('2048 Bytes')
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
