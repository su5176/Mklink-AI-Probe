import { computed, readonly, ref } from 'vue'
import { useMklinkApi } from './useMklinkApi'
import { useSymbolCatalog } from './useSymbolCatalog'
import { useToast } from './useToast'
import { tr } from './useLanguage'
import {
  DESKTOP_SETTINGS_CHANGED_EVENT,
  isSameFileSourcePath,
  isSymbolFilePath,
  loadDesktopSettings,
  saveDesktopSettings,
} from '../lib/desktopSettings'
import { pickSymbolFile } from '../lib/filePicker'
import type { AxlStatus } from '../types/mklink'

const connecting = ref(false)
const disconnecting = ref(false)
const loadingSymbols = ref(false)
const connectionError = ref('')
const symbolPath = ref('')
const symbolDisplayPath = ref('')
let settingsListenerReady = false

function syncSettings(): void {
  if (typeof window === 'undefined') return
  const settings = loadDesktopSettings(window.localStorage)
  symbolPath.value = settings.symbolPath
  symbolDisplayPath.value = settings.symbolDisplayPath || settings.symbolPath
}

function ensureSettingsListener(): void {
  if (settingsListenerReady || typeof window === 'undefined') return
  settingsListenerReady = true
  syncSettings()
  window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, syncSettings)
}

export function useDashboardSetup() {
  ensureSettingsListener()
  const api = useMklinkApi()
  const catalog = useSymbolCatalog()
  const toast = useToast()

  async function quickConnect(): Promise<boolean> {
    if (api.deviceStatus.value.connected || connecting.value) return true
    connecting.value = true
    connectionError.value = ''
    try {
      const path = symbolPath.value.trim()
      await api.connectDevice({
        restore_last: true,
        ...(isSymbolFilePath(path) ? { axf: path } : {}),
      })
      toast.success(tr('设备已连接', 'Device connected'))
      return true
    } catch (cause) {
      connectionError.value = cause instanceof Error ? cause.message : String(cause)
      toast.error(tr('连接失败：', 'Connection failed: ') + connectionError.value)
      return false
    } finally {
      connecting.value = false
    }
  }

  async function quickDisconnect(): Promise<boolean> {
    if (!api.deviceStatus.value.connected || disconnecting.value) return true
    disconnecting.value = true
    connectionError.value = ''
    try {
      await api.disconnectDevice()
      toast.info(tr('设备已断开，串口助手和 Modbus 不受影响', 'Device disconnected. Serial Assistant and Modbus keep running.'))
      return true
    } catch (cause) {
      connectionError.value = cause instanceof Error ? cause.message : String(cause)
      toast.error(tr('断开失败：', 'Disconnect failed: ') + connectionError.value)
      return false
    } finally {
      disconnecting.value = false
    }
  }

  function clearConnectionError(): void {
    connectionError.value = ''
  }

  async function parseSelectedSymbols(): Promise<boolean> {
    const path = symbolPath.value.trim()
    if (!isSymbolFilePath(path)) return false
    if (!api.deviceStatus.value.connected) {
      toast.info(tr('已保存符号文件，连接设备时将自动加载', 'Symbol file saved and will load when the device connects.'))
      return true
    }

    loadingSymbols.value = true
    try {
      const result = await api.parseAxf(path) as AxlStatus
      if (!result.loaded) throw new Error(result.error || tr('AXF / ELF 解析失败', 'AXF / ELF parsing failed'))
      if (!isSameFileSourcePath(path, result.axf_path)) {
        throw new Error(tr('后端未切换到所选符号文件', 'The backend did not switch to the selected symbol file'))
      }
      await catalog.ensureLoaded(true)
      toast.success(tr(`已加载 ${result.variable_count || 0} 个可读变量`, `Loaded ${result.variable_count || 0} readable variables`))
      return true
    } catch (cause) {
      toast.error(tr('符号加载失败：', 'Failed to load symbols: ') + (cause instanceof Error ? cause.message : String(cause)))
      return false
    } finally {
      loadingSymbols.value = false
    }
  }

  async function loadSymbolFile(): Promise<boolean> {
    if (loadingSymbols.value) return false
    loadingSymbols.value = true
    try {
      const selected = await pickSymbolFile()
      if (!selected) return false
      const source = typeof selected === 'string'
        ? { path: selected, displayPath: selected }
        : await api.uploadFileSource('symbol', selected).then(uploaded => ({
          path: uploaded.path,
          displayPath: uploaded.name || selected.name,
        }))
      const settings = loadDesktopSettings(window.localStorage)
      saveDesktopSettings(window.localStorage, {
        ...settings,
        symbolPath: source.path,
        symbolDisplayPath: source.displayPath === source.path ? '' : source.displayPath,
      })
      syncSettings()
    } catch (cause) {
      toast.error(tr('选择 AXF / ELF 失败：', 'Failed to select AXF / ELF: ') + (cause instanceof Error ? cause.message : String(cause)))
      return false
    } finally {
      loadingSymbols.value = false
    }
    return parseSelectedSymbols()
  }

  return {
    connecting: readonly(connecting),
    disconnecting: readonly(disconnecting),
    loadingSymbols: readonly(loadingSymbols),
    connectionError: readonly(connectionError),
    symbolPath: readonly(symbolPath),
    symbolDisplayPath: readonly(symbolDisplayPath),
    hasSymbolSource: computed(() => isSymbolFilePath(symbolPath.value)),
    quickConnect,
    quickDisconnect,
    clearConnectionError,
    loadSymbolFile,
    parseSelectedSymbols,
  }
}
