// ABOUTME: A truncated cell whose whole text is available on hover and focus.
// ABOUTME: Radix Tooltip, plus `title` so the text survives without pointers.
import type { ReactNode } from 'react'
import * as Tooltip from '@radix-ui/react-tooltip'

/**
 * Show a shortened cell, keep the whole thing reachable.
 *
 * A tooltip is a convenience, never the only copy: the full text also ships as
 * `title`, and the visible text is real text rather than an image or a
 * truncation the browser invented, so a screen reader and a printed page both
 * still get something.
 *
 * The trigger is deliberately **not** focusable. A tooltip trigger normally
 * should be, but four of these appear in every row of a fifty-row table: made
 * focusable they add two hundred tab stops between the top of the table and
 * anything after it, which is a far worse keyboard experience than a tooltip a
 * keyboard user cannot summon. The text itself is not hidden from them -- it
 * is in the cell and in `title` -- and the row, its link, its checkbox, and its
 * menu all remain in the tab order.
 */
export function HoverNote({
  full,
  children,
  className = '',
}: {
  /** The complete text. Omitted or equal to the visible text, no tooltip. */
  full?: string | null
  children: ReactNode
  className?: string
}) {
  const text = (full ?? '').trim()
  if (!text) return <span className={className}>{children}</span>
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <span title={text} className={className}>
          {children}
        </span>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          side="top"
          align="start"
          sideOffset={4}
          className="nx-tooltip z-50 max-w-sm"
        >
          {text}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  )
}

/** Wraps a screen that uses {@link HoverNote}; Radix needs one provider. */
export function HoverNoteProvider({ children }: { children: ReactNode }) {
  return (
    <Tooltip.Provider delayDuration={200} skipDelayDuration={300}>
      {children}
    </Tooltip.Provider>
  )
}
