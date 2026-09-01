import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import VersionHistoryPopover from './VersionHistoryPopover.vue'

describe('VersionHistoryPopover', () => {
  it('shows the current release notes and stable release history', async () => {
    const wrapper = mount(VersionHistoryPopover, {
      props: { version: '0.1.7', buildCommit: 'local' },
      attachTo: document.body,
    })

    expect(wrapper.get('[data-testid="app-version"]').text()).toContain('v0.1.7 · local')
    expect(wrapper.find('[data-testid="version-history-panel"]').exists()).toBe(false)

    await wrapper.trigger('mouseenter')

    const panel = wrapper.get('[data-testid="version-history-panel"]')
    expect(panel.text()).toContain('版本更新')
    expect(panel.text()).toContain('修复连接、在线读取与实时调试体验')
    expect(panel.text()).toContain('支持按地址读取 Flash、保存或清空数据')
    expect(panel.text()).toContain('修复 RTT、SuperWatch 文件保存和 RTOS Trace 时间轴刷新')
    expect(panel.text()).toContain('重新读取同路径的最新固件')
    expect(panel.text()).toContain('串口助手与 RTT 终端改用轻量数据通道')
    expect(panel.text()).toContain('串口锁文件命名兼容性')
    expect(panel.text()).toContain('每个实例使用独立后端和下载器连接')
    expect(panel.text()).toContain('新增启动画面')
    expect(panel.text()).toContain('完善在线烧录、实时采集、串口升级与 AI 安全边界')
    expect(panel.text()).toContain('YMODEM 文件传输')
    expect(panel.text()).toContain('Modbus RTU 工作台')
    expect(panel.text()).toContain('U 盘快速启动入口以 MKLink Web 上位机名称启动')
    expect(panel.text()).toContain('串行化 pyOCD 首次加载')
    expect(wrapper.get('.release-entry.current').findAll('li')).toHaveLength(5)
    expect(panel.text()).toContain('普通曲线')
    expect(panel.text()).toContain('停止后展开')
    expect(panel.text()).toContain('修复符号解析并完善调试资源协同')
    expect(panel.text()).toContain('匿名 struct/union 成员展开')
    expect(panel.text()).toContain('AI Skill 主动版本提醒')
    expect(panel.text()).toContain('完整 AXF 路径')
    expect(panel.text()).toContain('内置 pyelftools')
    expect(panel.text()).toContain('避免污染 JSON-RPC')
    expect(wrapper.findAll('[data-testid="release-entry"]')).toHaveLength(10)
    expect(wrapper.get('.release-entry.current').text()).toContain('v0.1.7')
    expect(wrapper.get('.current-badge').text()).toBe('当前版本')
    wrapper.unmount()
  })

  it('keeps mouse-wheel scrolling inside a long release history', async () => {
    const wrapper = mount(VersionHistoryPopover, {
      props: { version: '0.1.4', buildCommit: 'local' },
      attachTo: document.body,
    })
    await wrapper.trigger('mouseenter')
    const outsideWheel = vi.fn()
    document.addEventListener('wheel', outsideWheel)

    wrapper.get('[data-testid="version-history-panel"]').element.dispatchEvent(
      new WheelEvent('wheel', { bubbles: true, deltaY: 120 }),
    )

    expect(outsideWheel).not.toHaveBeenCalled()
    document.removeEventListener('wheel', outsideWheel)
    wrapper.unmount()
  })

  it('pins on click and closes on a second click or Escape', async () => {
    const wrapper = mount(VersionHistoryPopover, {
      props: { version: '0.1.3', buildCommit: 'f9f2f70' },
      attachTo: document.body,
    })
    const trigger = wrapper.get('[data-testid="app-version"]')

    await trigger.trigger('click')
    await wrapper.trigger('mouseleave')
    expect(wrapper.find('[data-testid="version-history-panel"]').exists()).toBe(true)
    expect(trigger.attributes('aria-expanded')).toBe('true')

    await trigger.trigger('click')
    expect(wrapper.find('[data-testid="version-history-panel"]').exists()).toBe(false)

    await trigger.trigger('click')
    await document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="version-history-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes a pinned history panel when the user clicks outside', async () => {
    const wrapper = mount(VersionHistoryPopover, {
      props: { version: '0.1.3', buildCommit: 'f9f2f70' },
      attachTo: document.body,
    })

    await wrapper.get('[data-testid="app-version"]').trigger('click')
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="version-history-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
