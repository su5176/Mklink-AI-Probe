import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RttTerminalPanel from './RttTerminalPanel.vue'

const mocks = vi.hoisted(() => ({
  terminalOptions: [] as Array<Record<string, unknown>>,
  terminals: [] as Array<{
    handler?: (event: KeyboardEvent) => boolean
    onData?: (data: string) => void
    dispatchKeyDown: (event: KeyboardEvent) => boolean
    paste: ReturnType<typeof vi.fn>
    selectAll: ReturnType<typeof vi.fn>
    selection: string
  }>,
  native: false,
  invoke: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: mocks.invoke,
  isTauri: () => mocks.native,
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    options: Record<string, unknown>
    instance: (typeof mocks.terminals)[number]

    constructor(options: Record<string, unknown>) {
      mocks.terminalOptions.push(options)
      this.options = { disableStdin: options.disableStdin }
      const instance: (typeof mocks.terminals)[number] = {
        dispatchKeyDown: (event: KeyboardEvent) => {
          const allowXtermDefault = instance.handler?.(event) !== false
          // xterm 6 maps an unhandled Ctrl+V to C0.SYN and forwards it to
          // onData before cancelling the browser's default paste action.
          if (allowXtermDefault && event.ctrlKey && event.key.toLowerCase() === 'v') {
            instance.onData?.('\x16')
          }
          return allowXtermDefault
        },
        paste: vi.fn((text: string) => this.instance.onData?.(text)),
        selectAll: vi.fn(),
        selection: '',
      }
      this.instance = instance
      mocks.terminals.push(this.instance)
    }

    loadAddon() {}
    open() {}
    attachCustomKeyEventHandler(handler: (event: KeyboardEvent) => boolean) {
      this.instance.handler = handler
    }
    onData(handler: (data: string) => void) {
      this.instance.onData = handler
      return { dispose() {} }
    }
    getSelection() { return this.instance.selection }
    paste(text: string) { this.instance.paste(text) }
    selectAll() { this.instance.selectAll() }
    clear() {}
    write() {}
    focus() {}
    dispose() {}
  },
}))

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit() {}
  },
}))

describe('RttTerminalPanel', () => {
  beforeEach(() => {
    mocks.terminalOptions.length = 0
    mocks.terminals.length = 0
    mocks.native = false
    mocks.invoke.mockReset()
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: vi.fn(),
        writeText: vi.fn(),
      },
    })
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn(() => true),
    })
  })

  it('treats both bare LF and CRLF as terminal newlines', () => {
    const wrapper = mount(RttTerminalPanel, {
      props: { inputEnabled: true },
    })

    expect(mocks.terminalOptions).toHaveLength(1)
    expect(mocks.terminalOptions[0].convertEol).toBe(true)
    wrapper.unmount()
  })

  it.each([
    { ctrlKey: true, metaKey: false },
    { ctrlKey: false, metaKey: true },
  ])('selects all terminal content for Ctrl/Cmd+A', modifiers => {
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: true } })
    const preventDefault = vi.fn()

    const handled = mocks.terminals[0].handler?.({
      type: 'keydown', key: 'a', altKey: false, preventDefault, ...modifiers,
    } as unknown as KeyboardEvent)

    expect(handled).toBe(false)
    expect(preventDefault).toHaveBeenCalledOnce()
    expect(mocks.terminals[0].selectAll).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('copies the current selection in the browser', async () => {
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: false } })
    mocks.terminals[0].selection = 'selected output'
    const preventDefault = vi.fn()

    const handled = mocks.terminals[0].handler?.({
      type: 'keydown', key: 'c', ctrlKey: true, metaKey: false, altKey: false, preventDefault,
    } as unknown as KeyboardEvent)
    await flushPromises()

    expect(handled).toBe(false)
    expect(preventDefault).toHaveBeenCalledOnce()
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('selected output')
    wrapper.unmount()
  })

  it('falls back to the legacy browser copy path when clipboard permission is denied', async () => {
    vi.mocked(navigator.clipboard.writeText).mockRejectedValue(new Error('permission denied'))
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: true } })
    mocks.terminals[0].selection = 'fallback copy'

    mocks.terminals[0].handler?.({
      type: 'keydown', key: 'c', ctrlKey: true, metaKey: false, altKey: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent)
    await flushPromises()

    expect(document.execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea[readonly]')).toBeNull()
    wrapper.unmount()
  })

  it('leaves Ctrl+C to xterm when there is no selection so it emits ETX', () => {
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: true } })
    const preventDefault = vi.fn()

    const handled = mocks.terminals[0].handler?.({
      type: 'keydown', key: 'c', ctrlKey: true, metaKey: false, altKey: false, preventDefault,
    } as unknown as KeyboardEvent)

    expect(handled).toBe(true)
    expect(preventDefault).not.toHaveBeenCalled()
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('uses the trusted browser paste event without clipboard read permission', async () => {
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: true } })
    const preventDefault = vi.fn()

    const allowXtermDefault = mocks.terminals[0].dispatchKeyDown({
      type: 'keydown', key: 'v', ctrlKey: true, metaKey: false, altKey: false, preventDefault,
    } as unknown as KeyboardEvent)
    expect(allowXtermDefault).toBe(false)
    expect(preventDefault).not.toHaveBeenCalled()
    expect(wrapper.emitted('input')).toBeUndefined()

    const paste = new Event('paste', { bubbles: true, cancelable: true })
    const stopPropagation = vi.spyOn(paste, 'stopPropagation')
    Object.defineProperty(paste, 'clipboardData', {
      value: { getData: vi.fn(() => 'pasted command\r') },
    })
    wrapper.get('.rtt-terminal-panel').element.dispatchEvent(paste)
    await flushPromises()

    expect(paste.defaultPrevented).toBe(true)
    expect(stopPropagation).toHaveBeenCalledOnce()
    expect(navigator.clipboard.readText).not.toHaveBeenCalled()
    expect(mocks.terminals[0].paste).toHaveBeenCalledWith('pasted command\r')
    expect(wrapper.emitted('input')).toEqual([['pasted command\r']])
    wrapper.unmount()
  })

  it('does not paste while terminal input is disabled', async () => {
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: false } })

    const handled = mocks.terminals[0].handler?.({
      type: 'keydown', key: 'v', ctrlKey: true, metaKey: false, altKey: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent)
    await flushPromises()

    expect(handled).toBe(false)
    expect(navigator.clipboard.readText).not.toHaveBeenCalled()
    expect(mocks.terminals[0].paste).not.toHaveBeenCalled()

    const paste = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(paste, 'clipboardData', {
      value: { getData: vi.fn(() => 'blocked paste') },
    })
    wrapper.get('.rtt-terminal-panel').element.dispatchEvent(paste)
    expect(paste.defaultPrevented).toBe(true)
    expect(mocks.terminals[0].paste).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('uses native clipboard commands in the Tauri package', async () => {
    mocks.native = true
    mocks.invoke.mockImplementation((command: string) => {
      if (command === 'clipboard_read_text') return Promise.resolve('native paste')
      return Promise.resolve()
    })
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: true } })
    mocks.terminals[0].selection = 'native copy'

    mocks.terminals[0].handler?.({
      type: 'keydown', key: 'c', ctrlKey: true, metaKey: false, altKey: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent)
    mocks.terminals[0].handler?.({
      type: 'keydown', key: 'v', ctrlKey: true, metaKey: false, altKey: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent)
    await flushPromises()

    expect(mocks.invoke).toHaveBeenCalledWith('clipboard_write_text', { text: 'native copy' })
    expect(mocks.invoke).toHaveBeenCalledWith('clipboard_read_text')
    expect(mocks.terminals[0].paste).toHaveBeenCalledWith('native paste')
    wrapper.unmount()
  })

  it('falls back to WebView clipboard APIs when a native command fails', async () => {
    mocks.native = true
    mocks.invoke.mockRejectedValue(new Error('clipboard temporarily busy'))
    vi.mocked(navigator.clipboard.readText).mockResolvedValue('webview paste')
    const wrapper = mount(RttTerminalPanel, { props: { inputEnabled: true } })
    mocks.terminals[0].selection = 'webview copy'

    mocks.terminals[0].handler?.({
      type: 'keydown', key: 'c', ctrlKey: true, metaKey: false, altKey: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent)
    mocks.terminals[0].handler?.({
      type: 'keydown', key: 'v', ctrlKey: true, metaKey: false, altKey: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent)
    await flushPromises()

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('webview copy')
    expect(navigator.clipboard.readText).toHaveBeenCalledOnce()
    expect(mocks.terminals[0].paste).toHaveBeenCalledWith('webview paste')
    wrapper.unmount()
  })
})
