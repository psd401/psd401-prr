// ABOUTME: Modal lifecycle tests for user-initiated and programmatic closes.
// ABOUTME: Prevents controlled close effects from firing a second onClose callback.
import { useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Modal } from './Modal'

describe('Modal', () => {
  it('wraps keyboard focus at both dialog boundaries', async () => {
    const user = userEvent.setup()
    render(
      <Modal open title="Case task" onClose={() => undefined}>
        <label>
          First field
          <input />
        </label>
        <button type="button">Last action</button>
      </Modal>,
    )
    const first = screen.getByLabelText('First field')
    const last = screen.getByRole('button', { name: 'Last action' })

    first.focus()
    await user.tab({ shift: true })
    expect(last).toHaveFocus()

    last.focus()
    await user.tab()
    expect(first).toHaveFocus()
  })

  it('does not report a second close when its controlled open prop becomes false', () => {
    const onClose = vi.fn()
    const { rerender } = render(
      <Modal open title="Case task" onClose={onClose}>
        Task details
      </Modal>,
    )

    rerender(
      <Modal open={false} title="Case task" onClose={onClose}>
        Task details
      </Modal>,
    )
    fireEvent(document.querySelector('dialog')!, new Event('close'))

    expect(onClose).not.toHaveBeenCalled()
  })

  it('reopens when Escape is followed by a reopen before the queued close event', async () => {
    // A browser closes the dialog and restores focus as soon as Escape is
    // handled, but only queues the `close` event for a later task. A keyboard
    // user who presses Enter on the restored trigger in between must get the
    // dialog back, not have that late event shut it again.
    const user = userEvent.setup()
    function Harness() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open task</button>
          <Modal open={open} title="Case task" onClose={() => setOpen(false)}>
            Task details
          </Modal>
        </>
      )
    }
    render(<Harness />)
    const trigger = screen.getByRole('button', { name: 'Open task' })
    await user.click(trigger)
    const dialog = document.querySelector('dialog')!
    expect(dialog.open).toBe(true)

    const cancel = new Event('cancel', { cancelable: true })
    fireEvent(dialog, cancel)
    expect(cancel.defaultPrevented).toBe(false)
    dialog.removeAttribute('open')

    await user.click(trigger)
    fireEvent(dialog, new Event('close'))

    expect(dialog.open).toBe(true)
  })
})
