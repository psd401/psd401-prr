// ABOUTME: Accessible modal dialog on the native <dialog> element, Nexus styled.
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
      // nx-modal draws the panel; the native dialog keeps focus, Escape and the
      // top layer. Its padding moves to the header and body so the title rule
      // runs edge to edge.
      className={`nx-modal m-auto w-full gap-0 p-0 ${size === 'wide' ? 'max-w-6xl' : 'max-w-lg'} backdrop:bg-scrim [&:not([open])]:hidden`}
    >
      <div className="border-b border-line px-6 py-4">
        <h2 id={titleId} className="text-2xl">{title}</h2>
      </div>
      <div className="p-6">{children}</div>
    </dialog>
  )
}
