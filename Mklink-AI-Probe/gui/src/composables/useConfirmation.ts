import { inject, onBeforeUnmount, onDeactivated, provide, ref, type InjectionKey } from 'vue'

type Confirm = (message: string) => Promise<boolean>
const confirmationKey: InjectionKey<Confirm> = Symbol('confirmation')

// Never fall back to window.confirm: desktop webviews may suppress native dialogs.
export function useConfirmation(): Confirm {
  return inject(confirmationKey, async () => false)
}

export function provideConfirmation() {
  const message = ref<string | null>(null)
  let resolve: ((confirmed: boolean) => void) | undefined
  function answer(confirmed: boolean) {
    const pending = resolve
    resolve = undefined
    message.value = null
    pending?.(confirmed)
  }
  const confirm: Confirm = text => {
    // A second request must not replace the risk the user is currently reading.
    if (resolve) return Promise.resolve(false)
    message.value = text
    return new Promise<boolean>(done => { resolve = done })
  }
  provide(confirmationKey, confirm)
  onBeforeUnmount(() => answer(false))
  onDeactivated(() => answer(false))
  return { message, confirm, answer }
}
