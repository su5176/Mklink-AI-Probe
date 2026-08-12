<script setup lang="ts">
import { Check, Moon, Palette, Sun } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { tr } from '../composables/useLanguage'
import {
  setTheme,
  theme,
  themeDefinitions,
  toggleThemePair,
  type AppTheme,
} from '../composables/useTheme'

const themeOptions = computed(() => [
  { value: 'porcelain' as const, label: tr('晨雾白瓷', 'Porcelain'), group: 'light' as const },
  { value: 'mica' as const, label: tr('云母银', 'Mica'), group: 'light' as const },
  { value: 'aqua' as const, label: tr('青釉光', 'Aqua'), group: 'light' as const },
  { value: 'abyss' as const, label: tr('深海精工', 'Abyss'), group: 'dark' as const },
  { value: 'graphite' as const, label: tr('石墨悬浮', 'Graphite'), group: 'dark' as const },
  { value: 'aurora' as const, label: tr('极光电路', 'Aurora'), group: 'dark' as const },
])

const control = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const mode = computed(() => themeDefinitions[theme.value].mode)
const currentLabel = computed(() => themeOptions.value.find(option => option.value === theme.value)?.label || theme.value)
const lightThemes = computed(() => themeOptions.value.filter(option => option.group === 'light'))
const darkThemes = computed(() => themeOptions.value.filter(option => option.group === 'dark'))

function setOpen(next: boolean, returnFocus = false): void {
  open.value = next
  if (!next && returnFocus) void nextTick(() => trigger.value?.focus())
}

function chooseTheme(next: AppTheme): void {
  setTheme(next)
  setOpen(false)
}

function chooseThemePair(): void {
  toggleThemePair()
  setOpen(false)
}

function onDocumentPointerDown(event: PointerEvent): void {
  if (open.value && !control.value?.contains(event.target as Node)) setOpen(false)
}

function onDocumentKeyDown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && open.value) {
    event.preventDefault()
    setOpen(false, true)
  }
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocumentPointerDown)
  document.addEventListener('keydown', onDocumentKeyDown)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown)
  document.removeEventListener('keydown', onDocumentKeyDown)
})
</script>

<template>
  <div ref="control" class="theme-control">
    <button
      ref="trigger"
      type="button"
      class="theme-trigger"
      data-testid="theme-trigger"
      aria-haspopup="dialog"
      aria-controls="theme-panel"
      :aria-expanded="open"
      :title="tr('外观与主题', 'Appearance and theme')"
      :aria-label="tr(`当前主题：${currentLabel}`, `Current theme: ${currentLabel}`)"
      @click="setOpen(!open)"
    >
      <Palette :size="15" aria-hidden="true" />
      <span class="theme-swatch" aria-hidden="true"></span>
    </button>

    <Transition name="popover">
      <section
        v-if="open"
        id="theme-panel"
        class="theme-panel"
        role="dialog"
        :aria-label="tr('界面外观设置', 'Appearance settings')"
        data-testid="theme-panel"
      >
        <header class="theme-panel-header">
          <div>
            <span class="theme-eyebrow">APPEARANCE</span>
            <strong>{{ tr('界面主题', 'Interface theme') }}</strong>
          </div>
          <span>{{ currentLabel }}</span>
        </header>

        <div class="theme-group" role="group" :aria-label="tr('亮色主题', 'Light themes')">
          <span class="theme-group-label">{{ tr('亮色', 'Light') }}</span>
          <div class="theme-grid">
            <button
              v-for="option in lightThemes"
              :key="option.value"
              type="button"
              :class="['theme-option', { selected: theme === option.value }]"
              :aria-pressed="theme === option.value"
              :data-testid="`theme-option-${option.value}`"
              @click="chooseTheme(option.value)"
            >
              <span
                class="theme-preview"
                :style="{ backgroundColor: themeDefinitions[option.value].themeColor }"
                aria-hidden="true"
              ></span>
              <span>{{ option.label }}</span>
              <Check v-if="theme === option.value" :size="13" aria-hidden="true" />
            </button>
          </div>
        </div>

        <div class="theme-group" role="group" :aria-label="tr('暗色主题', 'Dark themes')">
          <span class="theme-group-label">{{ tr('暗色', 'Dark') }}</span>
          <div class="theme-grid">
            <button
              v-for="option in darkThemes"
              :key="option.value"
              type="button"
              :class="['theme-option', { selected: theme === option.value }]"
              :aria-pressed="theme === option.value"
              :data-testid="`theme-option-${option.value}`"
              @click="chooseTheme(option.value)"
            >
              <span
                class="theme-preview"
                :style="{ backgroundColor: themeDefinitions[option.value].themeColor }"
                aria-hidden="true"
              ></span>
              <span>{{ option.label }}</span>
              <Check v-if="theme === option.value" :size="13" aria-hidden="true" />
            </button>
          </div>
        </div>

        <button type="button" class="theme-pair-button" data-testid="theme-pair-toggle" @click="chooseThemePair">
          <Moon v-if="mode === 'light'" :size="14" aria-hidden="true" />
          <Sun v-else :size="14" aria-hidden="true" />
          <span>{{ mode === 'light' ? tr('切换配对暗色', 'Use paired dark theme') : tr('切换配对亮色', 'Use paired light theme') }}</span>
        </button>
      </section>
    </Transition>
  </div>
</template>

<style scoped>
.theme-control { position: relative; }
.theme-trigger {
  width: 36px;
  height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  border: 1px solid var(--control-border);
  border-radius: var(--radius-control);
  background: var(--surface);
  color: var(--text-soft);
  box-shadow: var(--shadow-control);
  cursor: pointer;
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard);
}
.theme-trigger:hover,
.theme-trigger[aria-expanded="true"] { border-color: var(--focus-ring); background: var(--surface-muted); color: var(--brand-text); }
.theme-trigger:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.theme-swatch { width: 7px; height: 7px; border-radius: 50%; background: var(--brand); box-shadow: 0 0 0 2px var(--surface); }
.theme-panel {
  position: absolute;
  z-index: 90;
  top: calc(100% + 8px);
  right: 0;
  width: 300px;
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-dialog);
  background: var(--surface-raised);
  color: var(--text);
  box-shadow: var(--shadow-popover);
}
.theme-panel-header { display: flex; align-items: end; justify-content: space-between; gap: 16px; padding-bottom: 2px; }
.theme-panel-header > div { display: grid; gap: 3px; }
.theme-panel-header strong { font-size: 14px; font-weight: 700; }
.theme-panel-header > span { overflow: hidden; color: var(--brand-text); font-size: 11px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.theme-eyebrow { color: var(--muted); font: 700 9px/1 var(--font-mono); letter-spacing: .13em; }
.theme-group { display: grid; gap: 6px; }
.theme-group-label { color: var(--muted); font-size: 10px; font-weight: 650; }
.theme-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.theme-option {
  min-width: 0;
  min-height: 54px;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) 13px;
  align-items: center;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--surface);
  color: var(--text-soft);
  font: 600 10px/1.25 var(--font-body);
  text-align: left;
  cursor: pointer;
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard), transform var(--motion-fast) var(--ease-standard);
}
.theme-option:hover { border-color: var(--line-strong); background: var(--surface-muted); color: var(--text); transform: translateY(-1px); }
.theme-option.selected { border-color: var(--focus-ring); background: var(--surface-selected); color: var(--nav-active-text); }
.theme-option:focus-visible,
.theme-pair-button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.theme-preview { width: 18px; height: 18px; border: 1px solid var(--line-strong); border-radius: 50%; box-shadow: inset 0 0 0 4px color-mix(in srgb, var(--brand) 16%, transparent); }
.theme-pair-button {
  width: 100%;
  min-height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid var(--control-border);
  border-radius: var(--radius-control);
  background: var(--field-bg);
  color: var(--text-soft);
  font: 600 11px var(--font-body);
  cursor: pointer;
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard);
}
.theme-pair-button:hover { border-color: var(--focus-ring); background: var(--surface-muted); color: var(--brand-text); }
.popover-enter-active,
.popover-leave-active { transition: opacity var(--motion-fast) var(--ease-standard), transform var(--motion-fast) var(--ease-standard); transform-origin: top right; }
.popover-enter-from,
.popover-leave-to { opacity: 0; transform: translateY(-4px) scale(.985); }
@media (max-width: 720px) {
  .theme-panel { position: fixed; top: 104px; right: 8px; left: 8px; width: auto; }
}
@media (prefers-reduced-motion: reduce) {
  .theme-option:hover { transform: none; }
}
</style>
