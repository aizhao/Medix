// Focus containment and Escape handling for stacked dialogs.
const stack = []
export const dialog = {
  mounted(el, binding) {
    const previous = document.activeElement
    const focusable = () => [...el.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select, textarea, summary, [tabindex="0"]')].filter(node => node.getClientRects().length)
    const handler = event => {
      if (stack.at(-1) !== el) return
      if (event.key === 'Escape') { event.preventDefault(); binding.value?.(); return }
      if (event.key !== 'Tab') return
      const items = focusable(), first = items[0], last = items.at(-1)
      if (!first) { event.preventDefault(); return }
      if (event.shiftKey && (document.activeElement === first || !el.contains(document.activeElement))) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !el.contains(document.activeElement))) { event.preventDefault(); first.focus() }
    }
    stack.push(el)
    el._dialogCleanup = () => { document.removeEventListener('keydown', handler); stack.splice(stack.indexOf(el), 1); if (previous?.isConnected) previous.focus() }
    document.addEventListener('keydown', handler)
    queueMicrotask(() => focusable()[0]?.focus())
  },
  unmounted(el) { el._dialogCleanup?.() },
}
