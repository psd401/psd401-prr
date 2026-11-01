// ABOUTME: Vitest setup — jest-dom matchers plus a <dialog> polyfill shim
// ABOUTME: (jsdom lacks showModal/close used by the Modal component).
import '@testing-library/jest-dom/vitest'

if (typeof HTMLDialogElement !== 'undefined') {
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
    this.open = true
  }
  HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
    this.open = false
    this.dispatchEvent(new Event('close'))
  }
}
