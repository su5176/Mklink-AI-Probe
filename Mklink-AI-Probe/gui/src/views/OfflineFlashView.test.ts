import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import OfflineFlashView from './OfflineFlashView.vue'
import router from '../router'

const offlineMocks = vi.hoisted(() => ({
  getStatus: vi.fn(),
  listAlgorithms: vi.fn(),
  getSecurityStatus: vi.fn(),
  preview: vi.fn(),
  deploy: vi.fn(),
  trigger: vi.fn(),
}))

const onlineMocks = vi.hoisted(() => ({
  searchTargets: vi.fn(),
  installPack: vi.fn(),
}))

const pickerMocks = vi.hoisted(() => ({ pickFlmFile: vi.fn() }))
vi.mock('../lib/filePicker', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/filePicker')>(),
  pickFlmFile: pickerMocks.pickFlmFile,
}))

vi.mock('../composables/useOfflineFlashApi', () => ({
  useOfflineFlashApi: () => offlineMocks,
}))

vi.mock('../composables/useOnlineFlashApi', () => ({
  useOnlineFlashApi: () => onlineMocks,
}))

describe('OfflineFlashView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const stored = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => stored.get(key) ?? null,
      setItem: (key: string, value: string) => { stored.set(key, value) },
      removeItem: (key: string) => { stored.delete(key) },
      clear: () => { stored.clear() },
    })
    offlineMocks.getStatus.mockResolvedValue({
      available: true,
      disk_path: 'TEST_DISK',
      python_dir: 'TEST_DISK/python',
      flm_dir: 'TEST_DISK/FLM',
    })
    offlineMocks.listAlgorithms.mockResolvedValue([])
    offlineMocks.getSecurityStatus.mockResolvedValue({
      model: 'V3', part_number: '', supported: false,
      unlock_supported: false, lock_supported: false, family: '',
      reason: '该器件尚未通过脱机加锁/解锁真机验证',
      unlock_erases_flash: false, reversible_lock: false,
      voltage_options_mv: [], default_voltage_mv: null,
    })
    onlineMocks.searchTargets.mockResolvedValue([])
    onlineMocks.installPack.mockResolvedValue({ result: { status: 'installed' }, events: [] })
    offlineMocks.deploy.mockResolvedValue({
      status: 'deployed',
      model: 'V4',
      script_name: 'factory-download.py',
      files: ['python/factory-download.py', 'firmware.hex'],
    })
    offlineMocks.preview.mockResolvedValue({
      model: 'V4',
      script_name: 'factory-download.py',
      script: '# generated preview',
    })
    offlineMocks.trigger.mockResolvedValue({ status: 'completed', lines: ['offline download finished'] })
    vi.stubGlobal('confirm', vi.fn(() => true))
  })

  it('registers a top-level offline flash route', () => {
    const route = router.getRoutes().find(candidate => candidate.name === 'offline-flash')
    expect(route?.path).toBe('/offline-flash')
  })

  it('requires a manual model selection and forces the V2/V3 script name', async () => {
    const wrapper = mount(OfflineFlashView)
    await flushPromises()

    expect(wrapper.text()).not.toContain('识别版本')
    expect(wrapper.get('[data-testid="offline-model"]').element).toHaveProperty('value', '')
    await wrapper.get('[data-testid="offline-model"]').setValue('V3')
    expect(wrapper.text()).toContain('offline_download.py')
    expect(wrapper.get<HTMLInputElement>('[data-testid="offline-script-name"]').element.value).toBe('offline_download.py')
    expect(wrapper.get('[data-testid="offline-script-name"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('option[value="auto"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="offline-deploy"]').attributes('disabled')).toBeDefined()
  })

  it('provides multi-file firmware selection and editable BIN address and FLM bases', () => {
    const source = readFileSync('src/views/OfflineFlashView.vue', 'utf8')

    expect(source).toContain('multiple accept=".bin,.hex"')
    expect(source).toContain('v-model="item.base_address"')
    expect(source).toContain('v-model="item.flash_base"')
    expect(source).toContain('v-model="item.ram_base"')
    expect(source).toContain('自动烧录次数')
    expect(source).toContain('SWD 速率')
    expect(source).toContain('添加本地 FLM')
    expect(source).toContain('建议流程')
  })

  it.each(['desktop', 'usb', 'browser'])('allows preview with a manually selected %s FLM source', async kind => {
    offlineMocks.getStatus.mockResolvedValue({ available: true, disk_path: 'G:\\', python_dir: 'G:\\python', flm_dir: 'G:\\FLM' })
    const source = kind === 'browser' ? new File(['algorithm'], 'Device.FLM')
      : kind === 'usb' ? 'G:\\FLM\\Device.FLM' : 'C:\\Users\\test\\Desktop\\Device.FLM'
    pickerMocks.pickFlmFile.mockResolvedValue(source)
    const wrapper = mount(OfflineFlashView)
    try {
      await flushPromises()
      await wrapper.get('[data-testid="offline-model"]').setValue('V4')
      await wrapper.get('[data-testid="offline-add-flm"]').trigger('click')
      await flushPromises()
      const input = wrapper.get('input[type="file"][multiple]')
      Object.defineProperty(input.element, 'files', { configurable: true, value: [new File(['hex'], 'firmware.hex')] })
      await input.trigger('change')
      expect(wrapper.find('[data-testid="offline-selection-warning"]').exists()).toBe(false)
      const previewButton = wrapper.findAll('button').find(button => button.text() === '生成预览')!
      expect(previewButton.attributes('disabled')).toBeUndefined()
      await previewButton.trigger('click')
      await flushPromises()
      expect(offlineMocks.preview).toHaveBeenCalledOnce()
      const [payload] = offlineMocks.preview.mock.calls[0]
      expect(payload.algorithms).toEqual([expect.objectContaining({
        source_kind: 'upload', file_name: 'Device.FLM',
        source_path: kind === 'browser' ? null : source,
        upload_index: kind === 'browser' ? 0 : null,
      })])
      expect(offlineMocks.deploy).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([
    ['deploy', 'offline file operation failed: Device.FLM: file is in use', 'U 盘文件更新失败'],
    ['trigger', 'Unable to connect to MKLink CDC port', '无法连接下载器命令串口'],
    ['trigger', 'Unexpected device response', 'Unexpected device response'],
  ])('shows actionable details for %s errors: %s', async (operation, detail, expected) => {
    pickerMocks.pickFlmFile.mockResolvedValue(new File(['algorithm'], 'Device.FLM'))
    if (operation === 'deploy') offlineMocks.deploy.mockRejectedValueOnce(new Error(detail))
    else offlineMocks.trigger.mockRejectedValueOnce(new Error(detail))
    const wrapper = mount(OfflineFlashView)
    try {
      await flushPromises()
      await wrapper.get('[data-testid="offline-model"]').setValue('V4')
      await wrapper.get('[data-testid="offline-add-flm"]').trigger('click')
      await flushPromises()
      const input = wrapper.get('input[type="file"][multiple]')
      Object.defineProperty(input.element, 'files', { value: [new File(['hex'], 'firmware.hex')] })
      await input.trigger('change')
      await wrapper.get('[data-testid="offline-deploy"]').trigger('click')
      await flushPromises()
      if (operation === 'trigger') {
        await wrapper.get('[data-testid="offline-trigger"]').trigger('click')
        await flushPromises()
      }
      expect(wrapper.text()).toContain(expected)
      expect(wrapper.get('.technical-error').text()).toContain(detail)
    } finally { wrapper.unmount() }
  })

  it('searches target suggestions while typing and supports keyboard selection', async () => {
    vi.useFakeTimers()
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'STM32F103RE', vendor: 'STMicroelectronics', pack_id: 'Keil.STM32F1xx_DFP',
      pack_version: '2.4.1', installed: true, source: 'installed',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'f103', file_name: 'STM32F10x_512.FLM', flash_base: '0x08000000', ram_base: '0x20000000',
      source_kind: 'existing', source_token: null, origin: '内置', available: true, on_probe: true,
    }])
    const wrapper = mount(OfflineFlashView, { attachTo: document.body })
    await flushPromises()
    onlineMocks.searchTargets.mockClear()

    const input = wrapper.get('[data-testid="offline-target-search"]')
    await input.trigger('focus')
    await input.setValue('STM32F103R')
    await vi.advanceTimersByTimeAsync(151)
    await flushPromises()

    expect(onlineMocks.searchTargets).toHaveBeenCalledWith('STM32F103R', { limit: 30 })
    expect(input.attributes('aria-expanded')).toBe('true')
    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(wrapper.text()).toContain('已选器件')
    expect(wrapper.text()).toContain('STM32F103RE')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('shows security capability as loading before enabling a validated target', async () => {
    let resolveCapability: (value: unknown) => void = () => undefined
    offlineMocks.getSecurityStatus.mockImplementation(() => new Promise(resolve => { resolveCapability = resolve }))
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'STM32F103RE', vendor: 'STMicroelectronics', pack_id: null,
      pack_version: null, installed: true, source: 'builtin',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'f103', file_name: 'STM32F10x_512.FLM', flash_base: '0x08000000', ram_base: '0x20000000',
      source_kind: 'existing', source_token: null, origin: '内置', available: true, on_probe: true,
    }])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V3')
    await wrapper.get('.target-result').trigger('click')
    await Promise.resolve()

    expect(wrapper.text()).toContain('正在检查器件支持')
    resolveCapability({
      model: 'V3', part_number: 'STM32F103RE', supported: true,
      unlock_supported: true, lock_supported: true, family: 'stm32f103-rdp1', reason: '',
      unlock_erases_flash: true, reversible_lock: true,
      voltage_options_mv: [1800, 3300, 5000], default_voltage_mv: 3300,
    })
    await flushPromises()

    expect(wrapper.text()).toContain('加锁/解锁已验证')
    expect(wrapper.get('[data-testid="offline-unlock"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="offline-lock"]').attributes('disabled')).toBeUndefined()
  })

  it('loads dropped firmware into the offline workspace', async () => {
    const wrapper = mount(OfflineFlashView)
    await flushPromises()

    await wrapper.get('[data-testid="offline-firmware-drop-zone"]').trigger('drop', {
      dataTransfer: { files: [new File(['hex'], 'dropped.hex')] },
    })

    expect(wrapper.findAll('[data-testid="offline-firmware-row"]')).toHaveLength(1)
    expect(wrapper.get<HTMLInputElement>('.firmware-row .file-name').element.value).toBe('dropped.hex')
  })

  it('keeps same-range algorithms from different sources selectable', async () => {
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'DEVICE_A', vendor: 'Vendor', pack_id: 'Vendor.Device_DFP',
      pack_version: '1.0.0', installed: true, source: 'bundle',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([
      {
        id: 'builtin', file_name: 'Device.FLM',
        flash_base: '0x08000000', ram_base: '0x20000000', source_kind: 'pack',
        source_token: 'catalog:bundle:one', origin: '内置 Pack', available: true, on_probe: false,
      },
      {
        id: 'custom', file_name: 'Device.FLM',
        flash_base: '0x08000000', ram_base: '0x20000000', source_kind: 'pack',
        source_token: 'custom:one', origin: '用户 FLM', available: true, on_probe: false,
      },
    ])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()

    await wrapper.get('.target-result').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('[data-testid="offline-algorithm-row"]')).toHaveLength(2)
    expect(wrapper.text()).toContain('内置 Pack')
    expect(wrapper.text()).toContain('用户 FLM')
  })

  it('deploys only algorithms referenced by the flash sequence', async () => {
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'STM32F103RC', vendor: 'STMicroelectronics', pack_id: 'Keil.STM32F1xx_DFP',
      pack_version: '2.4.1', installed: true, source: 'installed',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([
      {
        id: 'used', file_name: 'STM32F10x_512.FLM', flash_base: '0x08000000', ram_base: '0x20000000',
        source_kind: 'existing', source_token: null, origin: 'MCU profile', available: true, on_probe: true,
      },
      {
        id: 'unused', file_name: 'STM32F10x_1024.FLM', flash_base: '0x08000000', ram_base: '0x20000000',
        source_kind: 'existing', source_token: null, origin: 'MCU profile', available: false, on_probe: false,
      },
    ])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V4')
    await wrapper.get('.target-result').trigger('click')
    await flushPromises()
    const input = wrapper.get('input[type="file"][multiple]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['hex'], 'firmware.hex')],
    })
    await input.trigger('change')
    await wrapper.get('select.compact-input').setValue('used')
    await wrapper.get('[data-testid="offline-deploy"]').trigger('click')
    await flushPromises()

    expect(offlineMocks.deploy).toHaveBeenCalledOnce()
    expect(offlineMocks.deploy.mock.calls[0][0].algorithms).toEqual([
      expect.objectContaining({ id: 'used', file_name: 'STM32F10x_512.FLM' }),
    ])
  })

  it('shows an actionable warning when the selected FLM is unavailable', async () => {
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'STM32F103RC', vendor: 'STMicroelectronics', pack_id: 'Keil.STM32F1xx_DFP',
      pack_version: '2.4.1', installed: true, source: 'installed',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'missing', file_name: 'STM32F10x_1024.FLM', flash_base: '0x08000000', ram_base: '0x20000000',
      source_kind: 'existing', source_token: null, origin: 'MCU profile', available: false, on_probe: false,
    }])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V4')
    await wrapper.get('.target-result').trigger('click')
    await flushPromises()
    const input = wrapper.get('input[type="file"][multiple]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['hex'], 'firmware.hex')],
    })
    await input.trigger('change')

    expect(wrapper.get('[data-testid="offline-selection-warning"]').text()).toContain('STM32F10x_1024.FLM')
    expect(wrapper.get('[data-testid="offline-algorithm-unavailable"]').text()).toContain('选择本地 FLM')
    expect(wrapper.get('[data-testid="offline-deploy"]').attributes('disabled')).toBeDefined()
  })

  it('triggers the deployed V4 script by its configured file name', async () => {
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'STM32F103RC', vendor: 'STMicroelectronics', pack_id: 'Keil.STM32F1xx_DFP',
      pack_version: '2.4.1', installed: true, source: 'installed',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'profile-stm32f1', file_name: 'STM32F10x_1024.FLM',
      flash_base: '0x08000000', ram_base: '0x20000000', source_kind: 'existing',
      source_token: null, origin: 'MCU profile', available: true, on_probe: true,
    }])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V4')

    expect(wrapper.get('[data-testid="offline-trigger"]').attributes('disabled')).toBeDefined()
    await wrapper.get('.target-result').trigger('click')
    await flushPromises()
    const input = wrapper.get('input[type="file"][multiple]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['hex'], 'firmware.hex')],
    })
    await input.trigger('change')
    await wrapper.get('[data-testid="offline-deploy"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="offline-trigger"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-testid="offline-trigger"]').trigger('click')
    await flushPromises()

    expect(confirm).not.toHaveBeenCalled()
    expect(offlineMocks.trigger).toHaveBeenCalledWith(
      'V4',
      'factory-download.py',
      expect.any(Function),
    )

    await wrapper.get('[data-testid="offline-model"]').setValue('V3')
    await flushPromises()

    expect(wrapper.get('[data-testid="offline-trigger"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('offline_download.py')
  })

  it('generates the preview automatically before deploying', async () => {
    offlineMocks.preview.mockResolvedValue({
      model: 'V4',
      script_name: 'factory-download.py',
      script: '# generated preview',
    })
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'STM32F103RC', vendor: 'STMicroelectronics', pack_id: 'Keil.STM32F1xx_DFP',
      pack_version: '2.4.1', installed: true, source: 'installed',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'profile-stm32f1', file_name: 'STM32F10x_1024.FLM',
      flash_base: '0x08000000', ram_base: '0x20000000', source_kind: 'existing',
      source_token: null, origin: 'MCU profile', available: true, on_probe: true,
    }])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V4')
    await wrapper.get('.target-result').trigger('click')
    await flushPromises()
    const input = wrapper.get('input[type="file"][multiple]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['hex'], 'firmware.hex')],
    })
    await input.trigger('change')

    await wrapper.get('[data-testid="offline-deploy"]').trigger('click')
    await flushPromises()

    expect(offlineMocks.preview).toHaveBeenCalledOnce()
    expect(offlineMocks.preview.mock.invocationCallOrder[0]).toBeLessThan(
      offlineMocks.deploy.mock.invocationCallOrder[0],
    )
    expect(wrapper.text()).toContain('# generated preview')
  })

  it('confirms security choices immediately and sends the validated V3 recipe', async () => {
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'GD32F303CET6', vendor: 'GigaDevice', pack_id: 'GigaDevice.GD32F30x_DFP',
      pack_version: '2.2.4', installed: true, source: 'installed',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'gd32-main', file_name: 'GD32F30x_HD.FLM',
      flash_base: '0x08000000', ram_base: '0x20000000', source_kind: 'existing',
      source_token: null, origin: '常用型号内置算法', available: true, on_probe: true,
    }])
    offlineMocks.getSecurityStatus.mockResolvedValue({
      model: 'V3', part_number: 'GD32F303CET6', supported: true,
      unlock_supported: true, lock_supported: true, family: 'gd32f303xe-spc', reason: '',
      unlock_erases_flash: true, reversible_lock: true,
      voltage_options_mv: [1800, 3300, 5000], default_voltage_mv: 3300,
    })
    offlineMocks.deploy.mockResolvedValue({
      status: 'deployed', model: 'V3', script_name: 'offline_download.py',
      files: ['python/offline_download.py'],
    })
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V3')
    await wrapper.get('.target-result').trigger('click')
    await flushPromises()

    const unlock = wrapper.get<HTMLInputElement>('[data-testid="offline-unlock"]')
    await unlock.setValue(true)
    expect(wrapper.get('[role="alertdialog"]').text()).toContain('永久删除芯片全部 Flash 数据')
    expect(unlock.element.checked).toBe(false)
    await wrapper.get('[data-testid="confirmation-accept"]').trigger('click')
    await flushPromises()
    expect(unlock.element.checked).toBe(true)

    const voltage = wrapper.get<HTMLSelectElement>('[data-testid="offline-security-voltage"]')
    expect(voltage.findAll('option').map(option => option.text())).toEqual(['1.8 V', '3.3 V', '5 V'])
    await voltage.setValue('5000')
    expect(wrapper.get('[role="alertdialog"]').text()).toContain('全部负载均可承受 5V')
    await wrapper.get('[data-testid="confirmation-accept"]').trigger('click')
    await flushPromises()
    expect(voltage.element.value).toBe('5000')

    const lock = wrapper.get<HTMLInputElement>('[data-testid="offline-lock"]')
    await lock.setValue(true)
    expect(wrapper.get('[role="alertdialog"]').text()).toContain('下载成功后写入读保护')
    expect(wrapper.get('[role="alertdialog"]').text()).toContain('5V 可能损坏不耐受的目标板')
    await wrapper.get('[data-testid="confirmation-accept"]').trigger('click')
    await flushPromises()
    expect(lock.element.checked).toBe(true)
    expect(confirm).not.toHaveBeenCalled()

    const input = wrapper.get('input[type="file"][multiple]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['hex'], 'customer.hex')],
    })
    await input.trigger('change')

    const eraseAll = wrapper.get<HTMLInputElement>('[data-testid="offline-erase-all"]')
    await eraseAll.setValue(true)
    expect(wrapper.get('[role="alertdialog"]').text()).toContain('未被所选固件覆盖的引导程序、参数和用户数据')
    expect(eraseAll.element.checked).toBe(false)
    await wrapper.get('[data-testid="confirmation-accept"]').trigger('click')
    await flushPromises()
    expect(eraseAll.element.checked).toBe(true)

    await wrapper.get('[data-testid="offline-deploy"]').trigger('click')
    await flushPromises()

    expect(offlineMocks.deploy).toHaveBeenCalledOnce()
    expect(offlineMocks.deploy.mock.calls[0][0]).toEqual(expect.objectContaining({
      model: 'V3', target_part: 'GD32F303CET6',
      erase_all_before_download: true,
      unlock_before_download: true, lock_after_download: true,
      security_voltage_mv: 5000,
    }))
  })

  it('cancels a security choice and clears it when the model changes', async () => {
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'GD32F303CET6', vendor: 'GigaDevice', pack_id: null,
      pack_version: null, installed: true, source: 'builtin',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'gd32-main', file_name: 'GD32F30x_HD.FLM',
      flash_base: '0x08000000', ram_base: '0x20000000', source_kind: 'existing',
      source_token: null, origin: '内置', available: true, on_probe: true,
    }])
    offlineMocks.getSecurityStatus.mockResolvedValue({
      model: 'V3', part_number: 'GD32F303CET6', supported: true,
      unlock_supported: true, lock_supported: true, family: 'gd32f303xe-spc', reason: '',
      unlock_erases_flash: true, reversible_lock: true,
      voltage_options_mv: [3300], default_voltage_mv: 3300,
    })
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V3')
    await wrapper.get('.target-result').trigger('click')
    await flushPromises()

    const unlock = wrapper.get<HTMLInputElement>('[data-testid="offline-unlock"]')
    await unlock.setValue(true)
    await wrapper.get('[data-testid="confirmation-cancel"]').trigger('click')
    await flushPromises()
    expect(unlock.element.checked).toBe(false)

    await unlock.setValue(true)
    await wrapper.get('[data-testid="confirmation-accept"]').trigger('click')
    await flushPromises()
    expect(unlock.element.checked).toBe(true)
    await wrapper.get('[data-testid="offline-model"]').setValue('V4')
    await flushPromises()
    expect(unlock.element.checked).toBe(false)
  })

  it('renders trigger output while the V4 command is still running', async () => {
    offlineMocks.preview.mockResolvedValue({
      model: 'V4', script_name: 'factory-download.py', script: '# preview',
    })
    offlineMocks.trigger.mockImplementation(async (_model, _script, onLine) => {
      onLine('erase started')
      await Promise.resolve()
      onLine('program finished')
      return { status: 'completed', lines: ['erase started', 'program finished'] }
    })
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'STM32F103RC', vendor: 'STMicroelectronics', pack_id: 'Keil.STM32F1xx_DFP',
      pack_version: '2.4.1', installed: true, source: 'installed',
    }])
    offlineMocks.listAlgorithms.mockResolvedValue([{
      id: 'profile-stm32f1', file_name: 'STM32F10x_1024.FLM',
      flash_base: '0x08000000', ram_base: '0x20000000', source_kind: 'existing',
      source_token: null, origin: 'MCU profile', available: true, on_probe: true,
    }])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V4')
    await wrapper.get('.target-result').trigger('click')
    await flushPromises()
    const input = wrapper.get('input[type="file"][multiple]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['hex'], 'firmware.hex')],
    })
    await input.trigger('change')
    await wrapper.get('[data-testid="offline-deploy"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="offline-trigger"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('.trigger-log').text()).toContain('erase started')
    expect(wrapper.get('.trigger-log').text()).toContain('program finished')
  })

  it('configures HPM BIN download without Pack or FLM algorithms', async () => {
    onlineMocks.searchTargets.mockResolvedValue([{
      part_number: 'HPM5301xEGx', vendor: 'HPMicro', pack_id: null,
      pack_version: null, installed: true, source: 'builtin',
    }])
    const wrapper = mount(OfflineFlashView)
    await flushPromises()
    await wrapper.get('[data-testid="offline-model"]').setValue('V4')

    await wrapper.get('.target-result').trigger('click')
    await flushPromises()
    const input = wrapper.get('input[type="file"][multiple]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['bin'], 'app.bin')],
    })
    await input.trigger('change')
    await wrapper.get('[data-testid="offline-deploy"]').trigger('click')
    await flushPromises()

    expect(onlineMocks.installPack).not.toHaveBeenCalled()
    expect(offlineMocks.listAlgorithms).not.toHaveBeenCalled()
    expect(offlineMocks.deploy).toHaveBeenCalledOnce()
    const payload = offlineMocks.deploy.mock.calls[0][0]
    expect(payload.target_part).toBe('HPM5301xEGx')
    expect(payload.board).toBe('hpm5301evklite')
    expect(payload.algorithms).toEqual([])
    expect(payload.firmwares[0].algorithm_id).toBe('')
    expect(payload.firmwares[0].base_address).toBe('0x80000400')
  })
})
