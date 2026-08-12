import { readonly, ref } from 'vue'

export type AppTheme = 'porcelain' | 'mica' | 'aqua' | 'abyss' | 'graphite' | 'aurora'
export type ThemeMode = 'light' | 'dark'

export type ThemeDefinition = {
  mode: ThemeMode
  pair: AppTheme
  themeColor: string
}

export const themeDefinitions: Record<AppTheme, ThemeDefinition> = {
  porcelain: { mode: 'light', pair: 'abyss', themeColor: '#f4f8f8' },
  mica: { mode: 'light', pair: 'graphite', themeColor: '#e8eded' },
  aqua: { mode: 'light', pair: 'aurora', themeColor: '#e6f2f3' },
  abyss: { mode: 'dark', pair: 'porcelain', themeColor: '#0b2529' },
  graphite: { mode: 'dark', pair: 'mica', themeColor: '#0e1415' },
  aurora: { mode: 'dark', pair: 'aqua', themeColor: '#010d0d' },
}

const STORAGE_KEY = 'mklink-theme'

function isTheme(value: string | null): value is AppTheme {
  return value !== null && Object.hasOwn(themeDefinitions, value)
}

function initialTheme(): AppTheme {
  try {
    const saved = typeof window !== 'undefined' ? window.localStorage.getItem(STORAGE_KEY) : null
    if (isTheme(saved)) return saved
  } catch {
    // Theme persistence is optional.
  }
  return 'porcelain'
}

const activeTheme = ref<AppTheme>(initialTheme())

function applyDocumentTheme(theme: AppTheme): void {
  if (typeof document === 'undefined') return
  const definition = themeDefinitions[theme]
  document.documentElement.dataset.theme = theme
  document.documentElement.dataset.mode = definition.mode
  document.documentElement.style.colorScheme = definition.mode
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', definition.themeColor)
}

applyDocumentTheme(activeTheme.value)

export const theme = readonly(activeTheme)

export function setTheme(next: AppTheme): void {
  activeTheme.value = next
  applyDocumentTheme(next)
  try { window.localStorage.setItem(STORAGE_KEY, next) } catch { /* persistence is optional */ }
}

export function toggleThemePair(): void {
  setTheme(themeDefinitions[activeTheme.value].pair)
}
