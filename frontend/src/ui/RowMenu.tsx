// ABOUTME: The per-row actions menu — Radix DropdownMenu in Nexus nx-menu clothing.
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
          className="nx-icon-btn nx-icon-btn--sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus data-[state=open]:bg-surface-sunken"
        >
          <span aria-hidden="true" className="text-base leading-none">⋯</span>
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="nx-menu z-50 min-w-44"
        >
          {items.map((item) => (
            <DropdownMenu.Item
              key={item.label}
              disabled={item.disabled}
              onSelect={item.onSelect}
              className="nx-menu__item outline-none data-[disabled]:cursor-default data-[disabled]:text-ink-disabled"
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
