<template>
  <div class="rtt-transmit-wrapper">
    <div :data-testid="testId('transmit-bar')" class="rtt-transmit-bar">
      <button
        :data-testid="testId('format')" class="format-toggle" type="button"
        :title="mode === 'text' ? tr('切换到 HEX 发送', 'Switch to HEX mode') : tr('切换到字符串发送', 'Switch to text mode')"
        :disabled="sending"
        @click="toggleMode"
      >{{ mode === 'text' ? 'Abc' : 'Hex' }}</button>
      <ArrowRight :data-testid="testId('direction')" class="direction-icon" :size="16" aria-hidden="true" />
      <input
        v-model="input" :data-testid="testId('input')" class="transmit-input" type="text"
        :placeholder="mode === 'text' ? tr('输入要发送的字符串', 'Enter text to send') : tr('输入十六进制字节，例如 AA 55', 'Enter hex bytes, e.g. AA 55')"
        :disabled="sending" autocomplete="off" spellcheck="false" @keydown="onKeydown"
      >
      <button
        :data-testid="testId('clear')" class="icon-button" type="button" :title="tr('清空输入', 'Clear input')"
        :disabled="sending || !input" @click="clearInput"
      ><Trash2 :size="16" /></button>
      <div class="history-control">
        <button
          :data-testid="testId('history')" class="history-button" type="button" :title="tr('发送历史', 'Send history')"
          :aria-expanded="historyOpen" :disabled="sending" @click="historyOpen = !historyOpen"
        ><History :size="16" /></button>
        <div v-if="historyOpen" class="history-menu" @click.stop>
          <button
            v-for="(entry, index) in settings.sendHistory" :key="`${entry.timestamp}-${index}`"
            :data-testid="`${testId('history-item')}-${index}`" type="button" @click="restoreHistory(entry)"
          >
            <span>{{ entry.text || tr('(空)', '(empty)') }}</span>
            <small>{{ entry.mode === 'text' ? 'Abc' : 'Hex' }} {{ endingLabel(entry.lineEnding) }}</small>
          </button>
          <span v-if="!settings.sendHistory.length" class="history-empty">{{ tr('暂无历史', 'No history') }}</span>
        </div>
      </div>
      <select
        v-model="lineEnding" :data-testid="testId('ending')" class="ending-select" :title="tr('发送尾端', 'Line ending')"
        :disabled="sending"
        @change="updateSettings()"
      >
        <option :value="''">{{ tr('无', 'None') }}</option>
        <option :value="'\r'">\r</option>
        <option :value="'\n'">\n</option>
        <option :value="'\r\n'">\r\n</option>
      </select>
      <button
        :data-testid="testId('send')" class="send-button" type="button" :title="tr('发送', 'Send')"
        :disabled="!canSend" @click="submit"
      ><Send :size="16" /><span>{{ tr('发送', 'Send') }}</span></button>
    </div>
    <div v-if="error" class="transmit-error" role="alert">{{ error }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowRight, History, Send, Trash2 } from '@lucide/vue'
import {
  MAX_SEND_HISTORY,
  type DesktopSettings,
  type RttLineEnding,
  type RttSendHistoryEntry,
  type RttTransmitMode,
} from '../../lib/desktopSettings'
import { encodeRttTransmit } from '../../lib/rttTransmit'
import { tr } from '../../composables/useLanguage'

const props = defineProps<{
  enabled: boolean
  settings: DesktopSettings
  send: (payload: Uint8Array) => Promise<void>
  idPrefix?: string
}>()
const emit = defineEmits<{ 'settings-change': [settings: DesktopSettings] }>()

const input = ref('')
const mode = ref<RttTransmitMode>(props.settings.transmitMode)
const lineEnding = ref<RttLineEnding>(props.settings.lineEnding)
const sending = ref(false)
const error = ref('')
const historyOpen = ref(false)
const hasMainPayload = computed(() => (
  mode.value === 'text'
    ? input.value.length > 0
    : input.value.replace(/[\x09-\x0d\x20]/g, '').length > 0
))
const canSend = computed(() => (
  props.enabled
  && !sending.value
  && (hasMainPayload.value || lineEnding.value.length > 0)
))

function testId(suffix: string): string {
  return `${props.idPrefix || 'rtt'}-${suffix}`
}

watch(() => props.settings, settings => {
  mode.value = settings.transmitMode
  lineEnding.value = settings.lineEnding
}, { deep: true })

function nextSettings(overrides: Partial<DesktopSettings> = {}): DesktopSettings {
  return {
    ...props.settings,
    transmitMode: mode.value,
    lineEnding: lineEnding.value,
    sendHistory: props.settings.sendHistory.map(entry => ({ ...entry })),
    ...overrides,
  }
}

function updateSettings(overrides: Partial<DesktopSettings> = {}): void {
  emit('settings-change', nextSettings(overrides))
}

function toggleMode(): void {
  mode.value = mode.value === 'text' ? 'hex' : 'text'
  error.value = ''
  updateSettings()
}

function endingLabel(value: RttLineEnding): string {
  if (value === '') return tr('无', 'None')
  return value.replace('\r', '\\r').replace('\n', '\\n')
}

function clearInput(): void {
  input.value = ''
  error.value = ''
}

function restoreHistory(entry: RttSendHistoryEntry): void {
  input.value = entry.text
  mode.value = entry.mode
  lineEnding.value = entry.lineEnding
  historyOpen.value = false
  error.value = ''
  updateSettings()
}

function historyAfterSuccess(
  submitted: Pick<RttSendHistoryEntry, 'text' | 'mode' | 'lineEnding'>,
): RttSendHistoryEntry[] {
  const entry = { ...submitted, timestamp: Date.now() }
  const previous = props.settings.sendHistory[0]
  if (
    previous?.text === entry.text
    && previous.mode === entry.mode
    && previous.lineEnding === entry.lineEnding
  ) return props.settings.sendHistory.map(item => ({ ...item }))
  return [entry, ...props.settings.sendHistory].slice(0, MAX_SEND_HISTORY)
}

async function submit(): Promise<void> {
  if (!canSend.value) return
  error.value = ''
  const submitted = {
    text: input.value,
    mode: mode.value,
    lineEnding: lineEnding.value,
  }
  let payload: Uint8Array
  try {
    payload = encodeRttTransmit(submitted.text, submitted.mode, submitted.lineEnding)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
    return
  }
  if (!payload.length) return

  sending.value = true
  historyOpen.value = false
  try {
    await props.send(payload)
    updateSettings({ sendHistory: historyAfterSuccess(submitted) })
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    sending.value = false
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.isComposing) return
  event.preventDefault()
  void submit()
}
</script>

<style scoped>
.rtt-transmit-wrapper { border-top: 1px solid var(--border, #d8dde6); background: var(--panel, #fff); }
.rtt-transmit-bar { min-height: 40px; display: grid; grid-template-columns: 42px 20px minmax(120px, 1fr) 32px 32px 66px 78px; align-items: center; gap: 4px; padding: 5px 8px; }
button, select, input { font: inherit; }
.format-toggle, .icon-button, .send-button { height: 30px; border: 1px solid var(--border, #cbd2dc); background: var(--surface, #fff); color: inherit; cursor: pointer; }
.format-toggle { font-size: 12px; font-weight: 600; }
.direction-icon { justify-self: center; color: var(--text-muted, #657084); }
.transmit-input { width: 100%; min-width: 0; height: 30px; border: 1px solid var(--border, #cbd2dc); padding: 0 8px; color: inherit; background: var(--surface, #fff); }
.icon-button { width: 32px; display: inline-grid; place-items: center; padding: 0; }
.history-control { position: relative; width: 32px; height: 30px; }
.history-button { width: 32px; height: 30px; display: grid; place-items: center; padding: 0; border: 1px solid var(--border, #cbd2dc); background: var(--surface, #fff); color: inherit; cursor: pointer; }
.history-menu { position: absolute; right: 0; bottom: 34px; z-index: 20; width: min(320px, 70vw); max-height: 240px; overflow: auto; border: 1px solid var(--border, #cbd2dc); background: var(--surface, #fff); box-shadow: 0 6px 18px rgb(0 0 0 / 14%); }
.history-menu button { width: 100%; display: flex; justify-content: space-between; gap: 12px; padding: 7px 9px; border: 0; border-bottom: 1px solid var(--border, #e5e7eb); background: transparent; color: inherit; text-align: left; cursor: pointer; }
.history-menu button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-menu small, .history-empty { color: var(--text-muted, #657084); white-space: nowrap; }
.history-empty { display: block; padding: 10px; text-align: center; }
.ending-select { height: 30px; min-width: 0; border: 1px solid var(--border, #cbd2dc); background: var(--surface, #fff); color: inherit; }
.send-button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; border-color: var(--brand); background: var(--brand); color: #fff; }
.send-button:hover:not(:disabled) { border-color: var(--brand-hover); background: var(--brand-hover); }
button:disabled { opacity: .45; cursor: not-allowed; }
.transmit-error { padding: 0 8px 6px; color: #dc2626; font-size: 12px; }
@media (max-width: 720px) {
  .rtt-transmit-bar { grid-template-columns: 42px 20px minmax(90px, 1fr) 32px 32px 58px 34px; }
  .send-button span { display: none; }
  .send-button { width: 34px; padding: 0; }
}
</style>
