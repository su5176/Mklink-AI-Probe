<script setup lang="ts">
import { CircleHelp, X } from '@lucide/vue'
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { tr } from '../composables/useLanguage'

defineProps<{ version: string; buildCommit: string }>()

const open = ref(false)
const trigger = ref<HTMLButtonElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
const dialog = ref<HTMLElement | null>(null)

function show(): void {
  open.value = true
  void nextTick(() => closeButton.value?.focus())
}

function close(): void {
  open.value = false
  void nextTick(() => trigger.value?.focus())
}

function onKeyDown(event: KeyboardEvent): void {
  if (!open.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return
  }
  if (event.key === 'Tab') {
    const focusable = dialog.value?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')
    if (!focusable?.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }
}

onMounted(() => document.addEventListener('keydown', onKeyDown))
onBeforeUnmount(() => document.removeEventListener('keydown', onKeyDown))
</script>

<template>
  <button
    ref="trigger"
    type="button"
    class="about-trigger"
    data-testid="about-trigger"
    :title="tr('关于 MKLink AI Probe', 'About MKLink AI Probe')"
    :aria-label="tr('关于 MKLink AI Probe', 'About MKLink AI Probe')"
    @click="show"
  >
    <CircleHelp :size="16" aria-hidden="true" />
  </button>

  <Teleport to="body">
    <div v-if="open" class="about-backdrop" data-testid="about-dialog" @pointerdown.self="close">
      <section ref="dialog" class="about-dialog" role="dialog" aria-modal="true" aria-labelledby="about-title">
        <button ref="closeButton" type="button" class="about-close" :aria-label="tr('关闭关于窗口', 'Close About dialog')" @click="close">
          <X :size="17" aria-hidden="true" />
        </button>
        <div class="about-hero">
          <img class="about-art" :src="'/brand/microkeen-about-hero.png'" alt="" />
          <div class="about-copy">
            <div class="about-company-lockup">
              <img class="about-company-logo" :src="'/brand/eternal-chip-logo-negative.png'" alt="ETERNAL CHIP" />
              <span>立芯恒方</span>
            </div>
            <p class="about-eyebrow">MICROKEEN · EMBEDDED DEVELOPMENT</p>
            <h2 id="about-title">MKLink AI Probe</h2>
            <p>{{ tr('智能调试与烧录工作站', 'Intelligent debug and flash workstation') }}</p>
          </div>
        </div>
        <div class="about-details">
          <div>
            <span>{{ tr('软件品牌', 'Software brand') }}</span>
            <strong>{{ tr('立芯恒方', 'ETERNAL CHIP') }}</strong>
          </div>
          <div>
            <span>{{ tr('硬件品牌', 'Hardware brand') }}</span>
            <strong>MicroKeen</strong>
          </div>
          <div>
            <span>{{ tr('应用版本', 'Application version') }}</span>
            <strong>v{{ version }}</strong>
          </div>
          <div>
            <span>{{ tr('构建版本', 'Build') }}</span>
            <strong>{{ buildCommit }}</strong>
          </div>
        </div>
        <p class="about-description">
          {{ tr('面向嵌入式开发的烧录、调试、数据采集与 AI 协作工具。', 'Flash, debug, data acquisition and AI collaboration tools for embedded development.') }}
        </p>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.about-trigger {
  width: var(--control-height);
  height: var(--control-height);
  display: grid;
  place-items: center;
  border: 1px solid var(--control-border);
  border-radius: var(--radius-control);
  background: var(--surface);
  color: var(--text-soft);
  box-shadow: var(--shadow-control);
  cursor: pointer;
  transition: color var(--motion-fast) var(--ease-standard), border-color var(--motion-fast) var(--ease-standard), background var(--motion-fast) var(--ease-standard);
}
.about-trigger:hover { border-color: var(--focus-ring); background: var(--surface-muted); color: var(--brand-text); }
.about-trigger:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.about-backdrop {
  position: fixed;
  z-index: 2000;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(3, 19, 21, .62);
  backdrop-filter: blur(3px);
}
.about-dialog {
  position: relative;
  width: min(760px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  overflow: auto;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-dialog);
  background: var(--surface-raised);
  color: var(--text);
  box-shadow: var(--shadow-raised);
}
.about-close {
  position: absolute;
  z-index: 2;
  top: 12px;
  right: 12px;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(181,216,219,.38);
  border-radius: var(--radius-control);
  background: rgba(11,37,41,.72);
  color: #edf5f4;
  cursor: pointer;
}
.about-close:focus-visible { outline: 2px solid #84bfc3; outline-offset: 2px; }
.about-hero { position: relative; min-height: 330px; overflow: hidden; border-radius: var(--radius-dialog) var(--radius-dialog) 0 0; background: #0b2529; }
.about-art { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.about-hero::after { content: ''; position: absolute; inset: 0; background: linear-gradient(90deg, rgba(5,28,32,.96) 0%, rgba(5,28,32,.76) 42%, rgba(5,28,32,.08) 72%); }
.about-copy { position: relative; z-index: 1; width: 46%; min-width: 260px; padding: 48px 0 42px 42px; color: #edf5f4; }
.about-company-logo { width: 172px; height: auto; object-fit: contain; }
.about-company-lockup { display: grid; justify-items: start; gap: 3px; }
.about-company-lockup span { margin-left: 2px; color: #c1d1cf; font-size: 11px; font-weight: 650; letter-spacing: .24em; }
.about-eyebrow { margin: 44px 0 8px; color: #9dcbcf; font: 700 10px/1.3 var(--font-mono); letter-spacing: .13em; }
.about-copy h2 { margin: 0; font-size: 30px; line-height: 1.15; letter-spacing: -.02em; }
.about-copy > p:last-child { margin-top: 8px; color: #c1d1cf; font-size: 13px; }
.about-details { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; border-bottom: 1px solid var(--line); background: var(--line); }
.about-details > div { display: grid; gap: 3px; padding: 15px 18px; background: var(--surface); }
.about-details span { color: var(--muted); font-size: 10px; }
.about-details strong { overflow: hidden; color: var(--text); font: 600 12px var(--font-mono); text-overflow: ellipsis; white-space: nowrap; }
.about-description { padding: 16px 18px 18px; color: var(--text-soft); font-size: 12px; }
@media (max-width: 620px) {
  .about-backdrop { padding: 12px; }
  .about-hero { min-height: 390px; }
  .about-art { object-position: 64% center; opacity: .62; }
  .about-hero::after { background: linear-gradient(180deg, rgba(5,28,32,.58), rgba(5,28,32,.96)); }
  .about-copy { width: auto; min-width: 0; padding: 42px 24px 28px; }
  .about-eyebrow { margin-top: 150px; }
  .about-details { grid-template-columns: 1fr; }
}
</style>
