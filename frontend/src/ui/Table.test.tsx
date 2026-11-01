// ABOUTME: Accessibility contract for the shared Table primitive.
// ABOUTME: A clickable row must be reachable and activatable from the keyboard.
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { Table } from './Table'
import { at } from '../test/at'

interface Row { name: string; age: number; note: string }

describe('Table', () => {
  it('lets keyboard users activate clickable rows', async () => {
    const onRowClick = vi.fn()
    const user = userEvent.setup()
    render(
      <Table
        columns={[{ header: 'Name', cell: (row: { name: string }) => row.name }]}
        rows={[{ name: 'Keyboard target' }]}
        rowKey={(row) => row.name}
        onRowClick={onRowClick}
      />,
    )

    const row = screen.getByText('Keyboard target').closest('tr')!
    expect(row).toHaveAttribute('tabindex', '0')
    await user.tab()
    expect(row).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onRowClick).toHaveBeenCalledWith({ name: 'Keyboard target' })
  })

  it('leaves a table with no sortable columns exactly as it was', () => {
    render(
      <Table
        columns={[{ header: 'Name', cell: (row: { name: string }) => row.name }]}
        rows={[{ name: 'Plain' }]}
        rowKey={(row) => row.name}
      />,
    )

    const header = screen.getByRole('columnheader', { name: 'Name' })
    expect(header).not.toHaveAttribute('aria-sort')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders a sortable column as a header button carrying aria-sort', async () => {
    const onSort = vi.fn()
    const user = userEvent.setup()
    render(
      <Table
        columns={[
          { header: 'Name', sortKey: 'name', cell: (row: Row) => row.name },
          { header: 'Age', sortKey: 'age', cell: (row: Row) => row.age },
          { header: 'Note', cell: (row: Row) => row.note },
        ]}
        rows={[{ name: 'Ada', age: 36, note: 'n/a' }]}
        rowKey={(row) => row.name}
        sort={{ key: 'name', direction: 'desc' }}
        onSort={onSort}
      />,
    )

    expect(screen.getByRole('columnheader', { name: /Name/ }))
      .toHaveAttribute('aria-sort', 'descending')
    // A sortable column that is not the current sort says so explicitly.
    expect(screen.getByRole('columnheader', { name: /Age/ }))
      .toHaveAttribute('aria-sort', 'none')
    // A column that cannot sort claims no sort state at all.
    expect(screen.getByRole('columnheader', { name: 'Note' }))
      .not.toHaveAttribute('aria-sort')

    await user.click(screen.getByRole('button', { name: /Age/ }))
    expect(onSort).toHaveBeenCalledWith('age')
  })

  it('reports ascending sort and reaches the header from the keyboard', async () => {
    const onSort = vi.fn()
    const user = userEvent.setup()
    render(
      <Table
        columns={[{ header: 'Name', sortKey: 'name', cell: (row: Row) => row.name }]}
        rows={[{ name: 'Ada', age: 36, note: 'n/a' }]}
        rowKey={(row) => row.name}
        sort={{ key: 'name', direction: 'asc' }}
        onSort={onSort}
      />,
    )

    expect(screen.getByRole('columnheader', { name: /Name/ }))
      .toHaveAttribute('aria-sort', 'ascending')
    await user.tab()
    expect(screen.getByRole('button', { name: /Name/ })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onSort).toHaveBeenCalledWith('name')
  })

  it('ignores a sort key when the caller cannot sort', () => {
    render(
      <Table
        columns={[{ header: 'Name', sortKey: 'name', cell: (row: Row) => row.name }]}
        rows={[{ name: 'Ada', age: 36, note: 'n/a' }]}
        rowKey={(row) => row.name}
      />,
    )

    expect(screen.getByRole('columnheader', { name: 'Name' }))
      .not.toHaveAttribute('aria-sort')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('names the table when the screen has more than one list', () => {
    render(
      <Table
        label="Cases"
        columns={[{ header: 'Name', cell: (row: Row) => row.name }]}
        rows={[{ name: 'Ada', age: 36, note: 'n/a' }]}
        rowKey={(row) => row.name}
      />,
    )

    expect(screen.getByRole('table', { name: 'Cases' })).toBeInTheDocument()
  })

  it('sizes to its content unless one column is allowed to grow', () => {
    const columns = [
      { header: 'Name', cell: (row: Row) => row.name },
      { header: 'Note', cell: (row: Row) => row.note },
    ]
    const rows = [{ name: 'Ada', age: 36, note: 'n/a' }]
    const { unmount } = render(
      <Table columns={columns} rows={rows} rowKey={(row) => row.name} />,
    )
    expect(screen.getByRole('table')).toHaveClass('min-w-max')
    unmount()

    render(
      <Table
        columns={[at(columns, 0), { ...at(columns, 1), grow: true }]}
        rows={rows}
        rowKey={(row) => row.name}
      />,
    )
    // With a growing column the table fits the page: that column wraps, and
    // the fixed columns after it stay on screen instead of scrolling away.
    expect(screen.getByRole('table')).not.toHaveClass('min-w-max')
    expect(screen.getByRole('columnheader', { name: 'Note' })).toHaveClass('w-full')
    expect(screen.getByRole('columnheader', { name: 'Name' }))
      .toHaveClass('whitespace-nowrap')
  })
})
