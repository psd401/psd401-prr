// ABOUTME: The workspace data table — TanStack Table for state, Radix for menus.
// ABOUTME: Server-ordered and server-paged; the browser never reorders a page.
import type { ReactNode } from 'react'
import {
  rowSortingFeature,
  tableFeatures,
  useTable,
} from '@tanstack/react-table'
import type { ColumnDef, RowData, SortingState } from '@tanstack/react-table'

import type { SortDirection, TableSort } from './Table'

/**
 * Sorting is declared but never computed here.
 *
 * `manualSorting` is the whole point: these lists are paged and ordered by the
 * server, so a column sorted in the browser would reorder fifty rows out of
 * four hundred and claim to have ordered the lot. The table renders the
 * control and the `aria-sort` state; the caller asks the server and puts the
 * answer in the URL.
 */
const features = tableFeatures({ rowSortingFeature })

const SORT_LABEL: Record<SortDirection, 'ascending' | 'descending'> = {
  asc: 'ascending',
  desc: 'descending',
}

/** Anything inside a row that owns its own click. */
const INTERACTIVE = 'a, button, input, select, textarea, label, [role="menuitem"]'

export interface DataTableColumn<T extends RowData> {
  /** Stable column id, and the `?sort=` key when `sortable` is set. */
  id: string
  /** Plain-text heading. Always text, so it stays readable to a screen
   *  reader even when `headerCell` renders a control in its place. */
  header: string
  /** Replaces the heading text with a control that carries its own
   *  accessible name (a select-all checkbox). `header` still labels it. */
  headerCell?: ReactNode
  cell: (row: T) => ReactNode
  /** Offer the sort control for this column, keyed by `id`. */
  sortable?: boolean
  align?: 'left' | 'right'
  /**
   * The one column that takes whatever width the others leave, wrapping
   * rather than widening the table. Without one the table sizes to its
   * content and scrolls sideways; with one it fits the page and only this
   * column gives, so the columns after it stay on screen.
   */
  grow?: boolean
}

export function DataTable<T extends RowData>({
  columns,
  rows,
  rowKey,
  label,
  sort,
  onSort,
  onRowActivate,
  rowLabel,
  rowEmphasis,
}: {
  columns: DataTableColumn<T>[]
  rows: T[]
  rowKey: (row: T) => string | number
  /** Accessible name for the table, for screens with more than one. */
  label?: string
  /** The current server-side ordering. */
  sort?: TableSort | null
  /** Called with a sortable column's id. The caller decides the next
   *  direction, because the caller is also what puts it in the URL. */
  onSort?: (key: string) => void
  /**
   * Open the row. The whole row is the target -- a case number is a small
   * thing to hit forty times a day -- but a click that landed on a link,
   * a checkbox, or a menu belongs to that control and is left alone.
   */
  onRowActivate?: (row: T) => void
  /** What activating this row does, for assistive technology. */
  rowLabel?: (row: T) => string
  /** Rows with something new on them, rendered the way unread mail is. */
  rowEmphasis?: (row: T) => boolean
}) {
  const sorting: SortingState = sort
    ? [{ id: sort.key, desc: sort.direction === 'desc' }]
    : []
  const tableColumns: ColumnDef<typeof features, T>[] = columns.map((column) => ({
    id: column.id,
    accessorFn: (row: T) => row,
    header: column.header,
    cell: (info) => column.cell(info.getValue<T>()),
    enableSorting: !!column.sortable && !!onSort,
  }))
  const table = useTable({
    features,
    columns: tableColumns,
    data: rows,
    manualSorting: true,
    state: { sorting },
    getRowId: (row: T) => String(rowKey(row)),
  })
  const byId = new Map(columns.map((column) => [column.id, column]))
  const fits = columns.some((column) => column.grow)

  function activate(row: T, target: EventTarget | null) {
    if (!onRowActivate) return
    if (target instanceof Element && target.closest(INTERACTIVE)) return
    onRowActivate(row)
  }

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
          {table.getHeaderGroups().map((headerGroup) => (
            <tr
              key={headerGroup.id}
              className="border-b border-driftwood bg-skylight text-left"
            >
              {headerGroup.headers.map((header) => {
                const column = byId.get(header.column.id)
                if (!column) return null
                const sortable = !!column.sortable && !!onSort
                const sorted =
                  sortable && sort && sort.key === column.id
                    ? sort.direction
                    : null
                return (
                  <th
                    key={header.id}
                    scope="col"
                    // Only a sortable column claims a sort state. "none" on a
                    // column that cannot sort would announce a control that
                    // is not there.
                    aria-sort={
                      sortable
                        ? sorted
                          ? SORT_LABEL[sorted]
                          : 'none'
                        : undefined
                    }
                    className={`${fits ? 'px-2.5' : 'px-3'} py-2.5 font-heading text-xs font-semibold uppercase tracking-[0.07em] text-pacific/80 ${column.align === 'right' ? 'text-right' : ''} ${fits && !column.grow ? 'whitespace-nowrap' : ''} ${column.grow ? 'w-full min-w-[12rem]' : ''}`}
                  >
                    {sortable ? (
                      <button
                        type="button"
                        onClick={() => onSort(column.id)}
                        className={
                          'inline-flex items-center gap-1 rounded font-heading text-xs font-semibold '
                          + 'uppercase tracking-[0.07em] focus-visible:outline-2 '
                          + 'focus-visible:outline-offset-2 focus-visible:outline-whulge '
                          + (sorted
                            ? 'text-pacific underline'
                            : 'text-pacific/80 hover:text-pacific hover:underline')
                        }
                      >
                        {column.headerCell ?? column.header}
                        {/* Decoration. `aria-sort` carries the state to
                            assistive technology and the underline carries it
                            without relying on colour. */}
                        <span aria-hidden="true" className="text-[0.7rem]">
                          {sorted === 'asc' ? '▲' : sorted === 'desc' ? '▼' : '↕'}
                        </span>
                      </button>
                    ) : (
                      column.headerCell ?? column.header
                    )}
                  </th>
                )
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((tableRow) => {
            const row = tableRow.original
            const emphasised = rowEmphasis?.(row) ?? false
            return (
              <tr
                key={tableRow.id}
                onClick={
                  onRowActivate
                    ? (event) => activate(row, event.target)
                    : undefined
                }
                onKeyDown={
                  onRowActivate
                    ? (event) => {
                        // Enter on the row itself. A key pressed inside a
                        // control in the row belongs to that control.
                        if (event.key !== 'Enter') return
                        if (event.target !== event.currentTarget) return
                        event.preventDefault()
                        onRowActivate(row)
                      }
                    : undefined
                }
                tabIndex={onRowActivate ? 0 : undefined}
                aria-label={onRowActivate ? rowLabel?.(row) : undefined}
                className={
                  'border-b border-seafoam last:border-0 '
                  + (emphasised ? 'bg-skylight ' : '')
                  + (onRowActivate
                    ? 'cursor-pointer hover:bg-seaglass/10 focus-visible:outline-2 '
                      + 'focus-visible:-outline-offset-2 focus-visible:outline-whulge'
                    : '')
                }
              >
                {columns.map((column) => (
                  <td
                    key={column.id}
                    className={`${fits ? 'px-2.5' : 'px-3'} py-3 text-pacific ${column.align === 'right' ? 'text-right' : ''} ${fits ? 'align-top' : ''} ${column.grow ? 'w-full min-w-[12rem]' : ''}`}
                  >
                    {column.cell(row)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
