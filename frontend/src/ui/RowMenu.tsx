// ABOUTME: The per-row actions menu — Radix DropdownMenu in PSD clothing.
// ABOUTME: Keyboard, focus return, and dismissal come from the primitive.
import type { ReactNode } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'

export interface RowMenuItem {
  label: string
  onSelect: () => void
  disabled?: boolean
}

/**
 * A compact menu of the things you can do to one row.
 *
 * Radix owns the parts that are easy to get wrong by hand and invisible when
 * you do: roving focus through the items, Escape and outside-click dismissal,
 * returning focus to the trigger afterwards, and the `aria-expanded` /
 * `aria-haspopup` wiring. What is ours is the appearance and the label, which
 * names the row so a screen-reader user hears which case's menu this is.
 */
export function RowMenu({
  label,
  items,
  children,
}: {
  /** Accessible name for the trigger, naming the row it belongs to. */
  label: string
  items: RowMenuItem[]
  /** Extra content below the items, for a destination link. */
  children?: ReactNode
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={label}
          className="inline-flex size-8 items-center justify-center rounded-lg border border-transparent text-pacific/80 transition-colors hover:border-driftwood hover:bg-skylight hover:text-pacific focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge data-[state=open]:border-driftwood data-[state=open]:bg-skylight"
        >
          <span aria-hidden="true" className="text-base leading-none">⋯</span>
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="z-50 min-w-[11rem] rounded-lg border border-driftwood bg-white p-1 shadow-lg"
        >
          {items.map((item) => (
            <DropdownMenu.Item
              key={item.label}
              disabled={item.disabled}
              onSelect={item.onSelect}
              className="cursor-pointer rounded px-2.5 py-1.5 text-sm text-pacific outline-none data-[highlighted]:bg-seaglass/15 data-[disabled]:cursor-default data-[disabled]:text-pacific/50"
            >
              {item.label}
            </DropdownMenu.Item>
          ))}
          {children}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}
