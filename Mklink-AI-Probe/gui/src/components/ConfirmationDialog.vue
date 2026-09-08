<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { tr } from '../composables/useLanguage'

defineProps<{ message: string }>()
const emit = defineEmits<{ answer: [confirmed: boolean] }>()
const cancel = ref<HTMLButtonElement | null>(null)
const accept = ref<HTMLButtonElement | null>(null)
let previousFocus: HTMLElement | null = null
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('answer', false)
  } else if (event.key === 'Tab') {
    event.preventDefault()
    ;(document.activeElement === cancel.value ? accept.value : cancel.value)?.focus()
  }
}
onMounted(() => {
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  cancel.value?.focus()
})
onBeforeUnmount(() => { if (previousFocus?.isConnected) previousFocus.focus() })
</script>

<template>
  <div class="confirmation-backdrop" @keydown="keydown" @click.self="emit('answer', false)">
    <section role="alertdialog" aria-modal="true" aria-labelledby="confirmation-title" aria-describedby="confirmation-message" class="confirmation-dialog">
      <h2 id="confirmation-title">{{ tr('请确认操作', 'Confirm operation') }}</h2>
      <p id="confirmation-message">{{ message }}</p>
      <div class="confirmation-buttons">
        <button ref="cancel" type="button" data-testid="confirmation-cancel" @click="emit('answer', false)">{{ tr('取消', 'Cancel') }}</button>
        <button ref="accept" type="button" data-testid="confirmation-accept" class="danger" @click="emit('answer', true)">{{ tr('确认', 'Confirm') }}</button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.confirmation-backdrop{position:fixed;inset:0;z-index:10000;display:grid;place-items:center;padding:20px;background:rgb(0 0 0 / 58%)}
.confirmation-dialog{box-sizing:border-box;width:min(520px,100%);padding:24px;border:1px solid #56616e;border-radius:10px;background:#222830;color:#f1f3f5;box-shadow:0 20px 70px #0008;text-align:left}
.confirmation-dialog h2{margin:0 0 14px;font-size:18px;color:inherit}.confirmation-dialog p{margin:0;line-height:1.7;font-size:14px;white-space:pre-wrap;color:inherit}
.confirmation-buttons{display:flex;justify-content:flex-end;gap:12px;margin-top:22px}.confirmation-buttons button{min-width:84px;padding:9px 16px;border:1px solid #78838f;border-radius:5px;background:#e8edf2;color:#222830;cursor:pointer;font-size:14px}.confirmation-buttons .danger{background:#c24e32;border-color:#f17a5e;color:white}.confirmation-buttons button:focus-visible{outline:3px solid #79bcff;outline-offset:3px}
</style>
