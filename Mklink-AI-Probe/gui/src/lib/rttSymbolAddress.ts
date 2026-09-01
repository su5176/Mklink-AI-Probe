import {
  isSameFileSourcePath,
  isSymbolFilePath,
  loadDesktopSettings,
  saveDesktopSettings,
  type DesktopSettings,
  type DesktopSettingsStorage,
} from './desktopSettings'
import type { RttFindResponse } from '../types/mklink'

export interface RttSymbolAddressRefresh {
  settings: DesktopSettings
  address: string
  stale: boolean
  error?: unknown
}

type FindRtt = (sourcePath?: string) => Promise<RttFindResponse>

const activeRefreshes = new WeakMap<object, symbol>()
let fallbackActiveRefresh: symbol | undefined

function setActiveRefresh(storage: DesktopSettingsStorage, request: symbol): void {
  if (storage && (typeof storage === 'object' || typeof storage === 'function')) {
    activeRefreshes.set(storage, request)
  } else {
    fallbackActiveRefresh = request
  }
}

function isActiveRefresh(storage: DesktopSettingsStorage, request: symbol): boolean {
  if (storage && (typeof storage === 'object' || typeof storage === 'function')) {
    return activeRefreshes.get(storage) === request
  }
  return fallbackActiveRefresh === request
}

function detectedAddress(result: RttFindResponse): string {
  if (!result.found || typeof result.addr !== 'string') return ''
  const address = result.addr.trim()
  return /^0x[0-9a-f]{1,8}$/i.test(address) ? address : ''
}

/** Cancel a pending automatic refresh when the user starts editing/searching. */
export function cancelRttAddressRefresh(storage: DesktopSettingsStorage): void {
  setActiveRefresh(storage, Symbol('cancelled RTT address refresh'))
}

/**
 * Refresh the shared RTT address after an AXF / ELF source becomes active.
 *
 * Selecting a different symbol file clears the previous image address in the
 * caller before this asynchronous diagnosis starts. Reconnecting the same file
 * keeps a user-entered address when automatic lookup is unavailable. A result
 * is applied only while this is still the newest request for the selected file.
 */
export async function refreshRttAddressForSymbol(
  storage: DesktopSettingsStorage,
  sourcePath: string,
  findRtt: FindRtt,
): Promise<RttSymbolAddressRefresh> {
  const source = sourcePath.trim()
  let settings = loadDesktopSettings(storage)
  if (!isSymbolFilePath(source) || !isSameFileSourcePath(settings.symbolPath, source)) {
    return { settings, address: settings.rttAddress, stale: true }
  }
  const initialAddress = settings.rttAddress

  const request = Symbol(source)
  setActiveRefresh(storage, request)
  let result: RttFindResponse
  try {
    result = await findRtt(source)
  } catch (error) {
    const latest = loadDesktopSettings(storage)
    const stale = !isActiveRefresh(storage, request)
      || !isSameFileSourcePath(latest.symbolPath, source)
      || latest.rttAddress !== initialAddress
    return {
      settings: latest,
      address: latest.rttAddress,
      stale,
      ...(stale ? {} : { error }),
    }
  }

  const latest = loadDesktopSettings(storage)
  if (
    !isActiveRefresh(storage, request)
    || !isSameFileSourcePath(latest.symbolPath, source)
    || latest.rttAddress !== initialAddress
  ) {
    return { settings: latest, address: latest.rttAddress, stale: true }
  }

  const address = detectedAddress(result)
  if (!address) {
    return { settings: latest, address: latest.rttAddress, stale: false }
  }
  settings = saveDesktopSettings(storage, { ...latest, rttAddress: address })
  return { settings, address, stale: false }
}
