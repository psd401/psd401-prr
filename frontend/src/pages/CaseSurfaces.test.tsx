// ABOUTME: Regression coverage for second-pass frontend workflow and accessibility findings.
// ABOUTME: Verifies canonical state copy, fee status, registry empty states, and filter semantics.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { Installments } from './Installments'
import { Preservations } from './Preservations'
import { RecordItems } from './RecordItems'
import { Registry } from './Registry'
import { Tasks } from './Tasks'
import { at } from '../test/at'

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as Response
}

function page(results: unknown[]) {
  return { count: results.length, next: null, previous: null, results }
}

function renderPage(ui: ReactNode, initialEntries: string[] = ['/']) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('collection-task deadline semantics', () => {
  it('uses the Pacific date and never marks completed work overdue', async () => {
    vi.setSystemTime(new Date('2026-07-31T00:30:00Z'))
    vi.stubGlobal('fetch', vi.fn(async () => response(page([
      {
        id: 1,
        request: '11111111-1111-4111-8111-111111111111',
        request_number: 'PRR-2026-0001',
        system_name: 'Google Workspace',
        system_slug: 'google-workspace',
        system_urgent_hold: false,
        kind: 'manual',
        state: 'pending',
        instructions: '',
        custodian_emails: [],
        search_terms: '',
        date_range_start: null,
        date_range_end: null,
        assigned_to_id: null,
        assigned_to_username: null,
        due_date: '2026-07-30',
        vault_matter_id: '',
        vault_hold_id: '',
        vault_export_id: '',
        created_at: '2026-07-01T12:00:00Z',
        completed_at: null,
      },
      {
        id: 2,
        request: '22222222-2222-4222-8222-222222222222',
        request_number: 'PRR-2026-0002',
        system_name: 'Google Workspace',
        system_slug: 'google-workspace',
        system_urgent_hold: false,
        kind: 'manual',
        state: 'complete',
        instructions: '',
        custodian_emails: [],
        search_terms: '',
        date_range_start: null,
        date_range_end: null,
        assigned_to_id: null,
        assigned_to_username: null,
        due_date: '2026-07-01',
        vault_matter_id: '',
        vault_hold_id: '',
        vault_export_id: '',
        created_at: '2026-07-01T12:00:00Z',
        completed_at: '2026-07-02T12:00:00Z',
      },
    ]))))

    renderPage(<Tasks roles={['reviewer']} />)

    const today = await screen.findByText('2026-07-30')
    expect(today).not.toHaveClass('text-red-800')
    expect(screen.getByText('2026-07-01')).not.toHaveClass('text-red-800')
  })
})

describe('record-item review state taxonomy', () => {
  it('offers every canonical state and presents non-responsive as a human label', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response(page([
      {
        id: '11111111-1111-4111-8111-111111111111',
        request_number: 'PRR-2026-0042',
        source_system: 'Google Drive',
        custodian: 'Records Office',
        original_filename: 'board-notes.pdf',
        sha256: '1234567890abcdef1234567890abcdef',
        review_state: 'non_responsive',
        reviewed_by_username: 'reviewer',
        redaction_count: 0,
        withholding_exemption_citations: [],
        created_at: '2026-07-30T12:00:00Z',
      },
    ])))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderPage(<RecordItems />)

    const filters = await screen.findByRole('group', { name: 'Review state filters' })
    for (const label of ['All', 'Unreviewed', 'Responsive', 'Non-responsive', 'Duplicate', 'Withheld (fully exempt)']) {
      expect(within(filters).getByRole('button', { name: label })).toBeInTheDocument()
    }
    for (const invalidLabel of ['Approved', 'Redacted', 'Released']) {
      expect(within(filters).queryByRole('button', { name: invalidLabel })).not.toBeInTheDocument()
    }

    const row = (await screen.findByText('board-notes.pdf')).closest('tr')!
    expect(within(row).getByText('Non-responsive')).toBeInTheDocument()
    expect(within(row).queryByText('non_responsive')).not.toBeInTheDocument()

    await user.click(within(filters).getByRole('button', { name: 'Duplicate' }))
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([input]) => String(input))
      expect(urls).toContain('/api/staff/record-items/?review_state=duplicate')
    })
  })
})

describe('installment fee status', () => {
  it('distinguishes a zero-fee release from a positive unpaid fee', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(page([
      {
        id: 1,
        request_number: 'PRR-2026-0001',
        sequence: 1,
        fee_amount: '0.00',
        fee_paid_at: null,
        released_at: null,
        download_expires_at: null,
      },
      {
        id: 2,
        request_number: 'PRR-2026-0002',
        sequence: 1,
        fee_amount: '12.50',
        fee_paid_at: null,
        released_at: null,
        download_expires_at: null,
      },
    ]))))

    renderPage(<Installments />)

    const freeRow = at(await screen.findAllByRole('row'), 1)
    expect(within(freeRow).getByText('Ready for release — no fee')).toBeInTheDocument()
    expect(within(freeRow).queryByText('Awaiting payment')).not.toBeInTheDocument()

    const feeRow = at(screen.getAllByRole('row'), 2)
    expect(within(feeRow).getByText('Awaiting payment')).toBeInTheDocument()
  })
})

describe('system registry states and controls', () => {
  it('explains an empty registry instead of rendering a header-only table', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(page([]))))

    renderPage(<Registry roles={['reviewer']} />)

    expect(await screen.findByText('No record systems configured.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('identifies each inline custodian email control by system name', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(page([
      {
        id: 7,
        slug: 'google-vault',
        name: 'Google Vault',
        category: 'communications',
        vendor: 'Google',
        integration: 'vault',
        custodian_title: 'Technology Director',
        custodian_email: 'technology@example.org',
        retention_days: 365,
        retention_note: '',
        urgent_hold: false,
        triage_keywords: ['email'],
        search_checklist: 'Search Gmail and Drive.',
        needs_verification: false,
        active: true,
      },
    ]))))

    renderPage(<Registry roles={['records_officer']} />)

    expect(await screen.findByRole('textbox', {
      name: 'Custodian email for Google Vault',
    })).toHaveAttribute('type', 'email')
  })

  it('shows the configured default owner for new system tasks', async () => {
    const recordSystem = {
      id: 7,
      slug: 'google-vault',
      name: 'Google Vault',
      category: 'communications',
      vendor: 'Google',
      integration: 'vault',
      custodian_title: 'Technology Director',
      custodian_email: 'technology@example.org',
      default_assignee_id: 11,
      default_assignee_username: 'liaison',
      retention_days: 365,
      retention_note: '',
      urgent_hold: false,
      triage_keywords: ['email'],
      search_checklist: 'Search Gmail and Drive.',
      needs_verification: false,
      active: true,
    }
    vi.stubGlobal('fetch', vi.fn(async () => response(page([recordSystem]))))

    renderPage(<Registry roles={['records_officer']} />)

    expect(await screen.findByRole('columnheader', { name: 'Default task owner' }))
      .toBeInTheDocument()
    expect(screen.getByText('liaison')).toBeInTheDocument()
  })
})

describe('request-scoped workflow links', () => {
  it('opens an exact collection task from its source-context link', async () => {
    const task = {
      id: 73,
      request: '11111111-1111-4111-8111-111111111111',
      request_number: 'PRR-2026-0042',
      system_name: 'Google Workspace',
      system_slug: 'google-workspace',
      system_urgent_hold: false,
      kind: 'manual',
      state: 'pending',
      instructions: 'Open this exact assigned search.',
      custodian_emails: [],
      search_terms: '',
      date_range_start: null,
      date_range_end: null,
      assigned_to_id: null,
      assigned_to_username: null,
      due_date: null,
      vault_matter_id: '',
      vault_hold_id: '',
      vault_export_id: '',
      vault_ingest: null,
      created_at: '2026-08-01T12:00:00Z',
      completed_at: null,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/collection-tasks/73/?request=PRR-2026-0042') {
        return response(task)
      }
      if (url === '/api/staff/collection-tasks/?request=PRR-2026-0042') {
        return response(page([]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage(
      <Tasks
        roles={['reviewer']}
        requestNumber="PRR-2026-0042"
      />,
      ['/requests/PRR-2026-0042/collection?task=73'],
    )

    const dialog = await screen.findByRole('dialog', { name: /task — request/i })
    expect(within(dialog).getByText('Open this exact assigned search.'))
      .toBeInTheDocument()
  })

  it('does not restore stale target data after a task mutation refetches the list', async () => {
    const user = userEvent.setup()
    let listCalls = 0
    const task = {
      id: 73,
      request: 42,
      request_number: 'PRR-2026-0042',
      system_name: 'Department Drive',
      system_slug: 'department-drive',
      system_urgent_hold: false,
      kind: 'manual',
      state: 'pending',
      instructions: 'Open this exact assigned search.',
      custodian_emails: [],
      search_terms: '',
      date_range_start: null,
      date_range_end: null,
      assigned_to_id: null,
      assigned_to_username: null,
      due_date: '2026-08-01',
      vault_matter_id: '',
      vault_hold_id: '',
      vault_export_id: '',
      vault_ingest: null,
      created_at: '2026-08-01T12:00:00Z',
      completed_at: null,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/collection-tasks/73/?request=PRR-2026-0042') {
        return response(task)
      }
      if (url === '/api/staff/collection-tasks/73/' && init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body))
        return response({
          ...task,
          due_date: body.due_date ?? '2026-09-01',
          assigned_to_id: body.assigned_to_id ?? null,
          assigned_to_username: body.assigned_to_id === 9 ? 'liaison' : null,
        })
      }
      if (url === '/api/staff/collection-tasks/?request=PRR-2026-0042') {
        listCalls += 1
        return response(page(
          listCalls > 1 ? [{ ...task, due_date: '2026-09-01' }] : [],
        ))
      }
      if (url === '/api/staff/requests/PRR-2026-0042/') {
        return response({ id: 42, number: 'PRR-2026-0042' })
      }
      if (url === '/api/staff/staff-users/') {
        return response(page([{ id: 9, username: 'liaison', display_name: 'Pat Liaison' }]))
      }
      return response(page([]))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage(
      <Tasks
        roles={['records_officer']}
        requestNumber="PRR-2026-0042"
      />,
      ['/requests/PRR-2026-0042/collection?task=73'],
    )

    const dialog = await screen.findByRole('dialog', { name: /task — request/i })
    const dueDate = within(dialog).getByLabelText('Due date')
    expect(dueDate).toHaveValue('2026-08-01')
    fireEvent.change(dueDate, { target: { value: '2026-09-01' } })

    await waitFor(() => expect(dueDate).toHaveValue('2026-09-01'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/collection-tasks/73/',
      expect.objectContaining({ method: 'PATCH' }),
    ))
    await waitFor(() => expect(listCalls).toBeGreaterThan(1))
    expect(dueDate).toHaveValue('2026-09-01')

    const assignTo = within(dialog).getByLabelText('Assign to')
    await user.selectOptions(assignTo, '9')
    const assignmentCalls = () => fetchMock.mock.calls.filter(([, init]) => (
      init?.method === 'PATCH'
      && JSON.parse(String(init.body)).assigned_to_id === 9
    ))
    expect(assignmentCalls()).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Save assignment' }))
    await waitFor(() => expect(assignmentCalls()).toHaveLength(1))
  })

  it('opens an exact record item even when it is outside the current list page', async () => {
    const itemId = '11111111-1111-4111-8111-111111111111'
    const item = {
      id: itemId,
      request_number: 'PRR-2026-0042',
      source_system: 'Google Drive',
      custodian: 'Records Office',
      original_filename: 'exact-record.pdf',
      sha256: 'a'.repeat(64),
      review_state: 'unreviewed',
      reviewed_by_username: null,
      reviewed_at: null,
      redaction_count: 0,
      second_review_required: false,
      second_reviewed_by_username: null,
      second_reviewed_at: null,
      redacted_artifact_ready: false,
      withholding_exemption_citations: [],
      created_at: '2026-08-01T12:00:00Z',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === `/api/staff/record-items/${itemId}/?request=PRR-2026-0042`) {
        return response(item)
      }
      if (url === '/api/staff/record-items/?request=PRR-2026-0042') {
        return response(page([]))
      }
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/redactions/')) return response(page([]))
      if (url.startsWith('/api/staff/exemptions/')) return response(page([]))
      if (url.endsWith('/advisory-responsiveness/')) {
        return response({ enabled: true, latest: null })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage(
      <RecordItems
        roles={['reviewer']}
        requestNumber="PRR-2026-0042"
      />,
      [`/requests/PRR-2026-0042/records?item=${itemId}`],
    )

    expect(await screen.findByRole('dialog', { name: /exact-record\.pdf/i }))
      .toBeInTheDocument()
  })

  it('keeps Collection tasks filtered to the originating request', async () => {
    const fetchMock = vi.fn(async () => response(page([])))
    vi.stubGlobal('fetch', fetchMock)

    renderPage(
      <Tasks
        roles={['reviewer']}
        requestNumber="PRR-2026-0042"
      />,
      ['/requests/PRR-2026-0042/collection'],
    )

    await screen.findByText('No collection tasks match the current filters.')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/collection-tasks/?request=PRR-2026-0042',
      expect.any(Object),
    )
  })

  it('keeps Record items filtered to the originating request', async () => {
    const fetchMock = vi.fn(async () => response(page([])))
    vi.stubGlobal('fetch', fetchMock)

    renderPage(
      <RecordItems requestNumber="PRR-2026-0042" />,
      ['/requests/PRR-2026-0042/records'],
    )

    await screen.findByText('No record items match these filters.')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/record-items/?request=PRR-2026-0042',
      expect.any(Object),
    )
  })

  it('lets a records officer create and assign a missing manual search task', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0042/') {
        return response({ id: 42, number: 'PRR-2026-0042' })
      }
      if (url === '/api/staff/staff-users/') {
        return response(page([{ id: 9, username: 'liaison', display_name: 'Pat Liaison' }]))
      }
      if (url === '/api/staff/record-systems/') {
        return response(page([{
          id: 7,
          name: 'Student information system',
          custodian_email: 'registrar@psd401.net',
          default_assignee_id: 9,
          search_checklist: 'Search the student record by name and date range.',
        }]))
      }
      if (url === '/api/staff/collection-tasks/' && init?.method === 'POST') {
        return response({ id: 101 })
      }
      return response(page([]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderPage(
      <Tasks
        roles={['records_officer']}
        requestNumber="PRR-2026-0042"
      />,
      ['/requests/PRR-2026-0042/collection'],
    )

    await user.click(await screen.findByRole('button', { name: 'Add search assignment' }))
    await user.selectOptions(screen.getByLabelText('Record system'), '7')
    expect(screen.getByLabelText('Assign to')).toHaveValue('9')
    expect(screen.getByLabelText('Instructions')).toHaveValue(
      'Search the student record by name and date range.',
    )
    await user.type(screen.getByLabelText('Due date'), '2026-08-14')
    await user.click(screen.getByRole('button', { name: 'Create assignment' }))

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find(
        ([url, init]) => String(url) === '/api/staff/collection-tasks/' && init?.method === 'POST',
      )
      expect(createCall).toBeDefined()
      expect(JSON.parse(String(createCall![1]!.body))).toMatchObject({
        request: 42,
        system: 7,
        assigned_to_id: 9,
        due_date: '2026-08-14',
        instructions: 'Search the student record by name and date range.',
        custodian_emails: ['registrar@psd401.net'],
      })
    })
  })
})

describe('filter pill semantics', () => {
  const cases: Array<{
    pageName: string
    groupName: string
    target: string
    renderPageContent: () => ReactNode
  }> = [
    {
      pageName: 'Record Items',
      groupName: 'Review state filters',
      target: 'Responsive',
      renderPageContent: () => <RecordItems />,
    },
    {
      pageName: 'Tasks',
      groupName: 'Task state filters',
      target: 'Pending',
      renderPageContent: () => <Tasks roles={['reviewer']} />,
    },
    {
      pageName: 'Preservations',
      groupName: 'Preservation state filters',
      target: 'pending',
      renderPageContent: () => <Preservations roles={['admin']} />,
    },
  ]

  it.each(cases)('$pageName announces the active filter', async ({ groupName, target, renderPageContent }) => {
    vi.stubGlobal('fetch', vi.fn(async () => response(page([]))))
    const user = userEvent.setup()

    renderPage(renderPageContent())

    const filters = await screen.findByRole('group', { name: groupName })
    const all = within(filters).getByRole('button', { name: 'All' })
    const targetButton = within(filters).getByRole('button', { name: target })
    expect(all).toHaveAttribute('aria-pressed', 'true')
    expect(targetButton).toHaveAttribute('aria-pressed', 'false')

    await user.click(targetButton)

    await waitFor(() => {
      const updatedFilters = screen.getByRole('group', { name: groupName })
      expect(within(updatedFilters).getByRole('button', { name: 'All' }))
        .toHaveAttribute('aria-pressed', 'false')
      expect(within(updatedFilters).getByRole('button', { name: target }))
        .toHaveAttribute('aria-pressed', 'true')
    })
  })

  it('stores the preservation filter in browser history', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(page([]))))
    const user = userEvent.setup()

    function HistoryProbe() {
      const location = useLocation()
      const navigate = useNavigate()
      return (
        <>
          <output aria-label="Current search">{location.search}</output>
          <button type="button" onClick={() => navigate(-1)}>Back</button>
          <Preservations
            roles={['records_officer']}
            requestNumber="PRR-2026-0042"
          />
        </>
      )
    }

    renderPage(<HistoryProbe />, ['/requests/PRR-2026-0042/collection'])
    const filters = await screen.findByRole('group', {
      name: 'Preservation state filters',
    })
    await user.click(within(filters).getByRole('button', { name: 'held' }))
    expect(screen.getByLabelText('Current search'))
      .toHaveTextContent('?preservation_state=held')

    const updatedFilters = await screen.findByRole('group', {
      name: 'Preservation state filters',
    })
    await user.click(
      within(updatedFilters).getByRole('button', { name: 'pending' }),
    )
    await waitFor(() => expect(
      screen.getByLabelText('Current search'),
    ).toHaveTextContent('?preservation_state=pending'))
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(
      screen.getByLabelText('Current search'),
    ).toHaveTextContent('?preservation_state=held'))
  })
})
