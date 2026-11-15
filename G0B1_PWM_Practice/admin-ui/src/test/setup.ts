import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

// jsdom lacks the native modal top-layer/focus implementation.
HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
if (!File.prototype.arrayBuffer) {
  File.prototype.arrayBuffer = function () {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as ArrayBuffer)
      reader.onerror = () => reject(new Error('read_failed'))
      reader.readAsArrayBuffer(this)
    })
  }
}
beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  // Every test is offline by default; explicitly mocked HTTP tests cannot escape.
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network access')))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })