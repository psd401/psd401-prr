// ABOUTME: Accessible modal dialog on the native <dialog> element, PSD styled.
// ABOUTME: Used for all confirm/reason-gated actions across the app.
import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from 'react'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export function Modal({
  open,
  title,
  onClose,
  dismissible = true,
  size = 'default',
  children,
}: {
  open: boolean
  title: string
  onClose: () => void
  dismissible?: boolean
  size?: 'default' | 'wide'
  children: ReactNode
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()

  const handleClose = () => {
    // Calling dialog.close() to reflect a controlled prop change also emits a
    // native close event. The parent already owns that state change, so do not
    // report it back as a second user-initiated close.
    if (!open) return
    const dialog = ref.current
    // The browser queues `close` for a later task. If the dialog is open again
    // by the time it arrives, it describes a close that is already over.
    if (dialog?.open) return
    if (!dismissible && dialog && !dialog.open) {
      dialog.showModal()
      return
    }
    onClose()
  }

  const trapFocus = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== 'Tab') return
    const dialog = ref.current
    if (!dialog) return
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    ).filter((element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true')
    const first = focusable[0]
    const last = focusable.at(-1)
    if (!first || !last) {
      event.preventDefault()
      dialog.focus()
      return
    }
    const active = document.activeElement
    if (event.shiftKey && (active === first || !dialog.contains(active))) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
      event.preventDefault()
      first.focus()
    }
  }

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onKeyDown={trapFocus}
      onClose={handleClose}
      onCancel={(event) => {
        if (!dismissible) {
          event.preventDefault()
          return
        }
        // Escape closes the native dialog and restores focus immediately, but
        // `close` fires a task later. Report the close now so the parent's
        // open state is false before the user can reopen from the trigger.
        onClose()
      }}
      onClick={(e) => {
        if (dismissible && e.target === ref.current) onClose()
      }}
      className={`m-auto w-full ${size === 'wide' ? 'max-w-6xl' : 'max-w-lg'} rounded-lg border border-driftwood bg-skylight p-0 shadow-xl backdrop:bg-pacific/50`}
    >
      <div className="border-b border-driftwood/60 bg-white px-5 py-3.5 rounded-t-lg">
        <h2 id={titleId} className="text-lg text-pacific">{title}</h2>
      </div>
      <div className="p-5">{children}</div>
    </dialog>
  )
}
