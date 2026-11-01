// ABOUTME: Data table with PSD styling — column defs in, striped rows out.
// ABOUTME: Used by every list screen for a consistent workspace feel.
import type { ReactNode } from 'react'

export type SortDirection = 'asc' | 'desc'

/** Which column the rows are ordered by, and which way. */
export interface TableSort {
  /** The `sortKey` of the ordered column. */
  key: string
  direction: SortDirection
}

export interface Column<T> {
  /** Column label, and the React key for the column. Always plain text so it
   *  stays readable to assistive technology even when `headerCell` renders a
   *  control in its place. */
  header: string
  /** Optional header content, for a column whose heading is itself a control
   *  (a select-all checkbox). It replaces the header text in the cell, so the
   *  control must carry its own accessible name; `header` still keys the
   *  column. */
  headerCell?: ReactNode
  cell: (row: T) => ReactNode
  align?: 'left' | 'right'
  /**
   * Makes the column sortable, and names the key the caller receives back.
   *
   * Sorting is the caller's job, not the table's: these lists are paged and
   * server-ordered, so a column sorted in the browser would reorder one page
   * of fifty and silently claim to have ordered the whole result. The table
   * renders the control and the `aria-sort` state; the caller asks the server.
   *
   * Ignored unless `onSort` is supplied, so a column definition can carry a
   * key for one screen and stay inert on another.
   */
  sortKey?: string
  /**
   * The one column that takes whatever width the others leave, wrapping its
   * text rather than widening the table. Without one, the table sizes to its
   * content and scrolls sideways; with one, it fits the page and only this
   * column gives, so the columns after it (and their actions) stay on screen.
   */
  grow?: boolean
}

const SORT_LABEL: Record<SortDirection, 'ascending' | 'descending'> = {
  asc: 'ascending',
  desc: 'descending',
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  sort,
  onSort,
  label,
}: {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string | number
  onRowClick?: (row: T) => void
  /** The current server-side ordering, when the caller sorts. */
  sort?: TableSort | null
  /**
   * Called with a sortable column's `sortKey` when its header is activated.
   * The caller decides the next direction; the table only reports the click,
   * because the caller is also what puts the ordering in the URL.
   */
  onSort?: (key: string) => void
  /** Accessible name for the table, for screens with more than one. */
  label?: string
}) {
  const fits = columns.some((c) => c.grow)
  return (
    // `relative` makes this scroller the containing block for absolutely
    // positioned cell content (`sr-only` text). Without it that content
    // escapes the horizontal clip and widens the whole page on a phone.
    <div className="relative overflow-x-auto rounded-lg border border-driftwood/60">
      <table
        aria-label={label}
        className={`w-full border-collapse bg-white text-sm ${fits ? '' : 'min-w-max'}`}
      >
        <thead>
          <tr className="border-b border-driftwood bg-skylight text-left">
            {columns.map((c) => {
              const sortable = !!c.sortKey && !!onSort
              const sorted = sortable && sort && sort.key === c.sortKey
                ? sort.direction
                : null
              return (
                <th
                  key={c.header}
                  scope="col"
                  // Only a sortable column claims a sort state. "none" on a
                  // column that cannot sort would announce a control that is
                  // not there.
                  aria-sort={
                    sortable ? (sorted ? SORT_LABEL[sorted] : 'none') : undefined
                  }
                  className={`${fits ? 'px-2.5' : 'px-3'} py-2.5 font-heading text-xs font-semibold uppercase tracking-[0.07em] text-pacific/80 ${c.align === 'right' ? 'text-right' : ''} ${fits && !c.grow ? 'whitespace-nowrap' : ''} ${c.grow ? 'w-full min-w-[12rem]' : ''}`}
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => c.sortKey && onSort(c.sortKey)}
                      className={
                        'inline-flex items-center gap-1 rounded font-heading text-xs font-semibold '
                        + 'uppercase tracking-[0.07em] focus-visible:outline-2 '
                        + 'focus-visible:outline-offset-2 focus-visible:outline-whulge '
                        + (sorted ? 'text-pacific underline' : 'text-pacific/80 hover:text-pacific hover:underline')
                      }
                    >
                      {c.headerCell ?? c.header}
                      {/* The arrow is decoration; `aria-sort` on the header
                          carries the state to assistive technology, and the
                          underline carries it without relying on colour. */}
                      <span aria-hidden="true" className="text-[0.7rem]">
                        {sorted === 'asc' ? '▲' : sorted === 'desc' ? '▼' : '↕'}
                      </span>
                    </button>
                  ) : (
                    c.headerCell ?? c.header
                  )}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={onRowClick ? (event) => {
                if (
                  event.target === event.currentTarget
                  && (event.key === 'Enter' || event.key === ' ')
                ) {
                  event.preventDefault()
                  onRowClick(row)
                }
              } : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              className={`border-b border-seafoam last:border-0 ${onRowClick ? 'cursor-pointer hover:bg-seaglass/10' : ''}`}
            >
              {columns.map((c) => (
                <td
                  key={c.header}
                  className={`${fits ? 'px-2.5' : 'px-3'} py-3 text-pacific ${c.align === 'right' ? 'text-right' : ''} ${fits ? 'align-top' : ''} ${c.grow ? 'w-full min-w-[12rem]' : ''}`}
                >
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
