// ABOUTME: Regression reproductions for workflow forms that expose opaque database IDs.
// ABOUTME: Staff must select recognizable assignees, requests, and installments.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ReactElement } from 'react'
import { ToastProvider } from '../ui'
import { CorrespondencePanel } from './CorrespondencePanel'
import { Tasks } from './Tasks'
import { TimeEntries } from './TimeEntries'
import { at } from '../test/at'

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function page(results: unknown[]) {
  return { count: results.length, next: null, previous: null, results }
}

function renderPage(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>{element}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Workflow selectors use names, not primary keys', () => {
  it('assigns a collection task by staff name instead of requiring a numeric user ID', async () => {
    // Selecting a task now always records it in the URL, so the list and the
    // detail endpoint both have to answer.
    const task = {
      id: 11,
      request: '11111111-1111-4111-8111-111111111111',
      request_number: 'PRR-2026-0042',
      system_name: 'Google Workspace',
      system_slug: 'google-workspace',
      system_urgent_hold: false,
      kind: 'manual',
      state: 'pending',
      instructions: 'Search the assigned custodians.',
      custodian_emails: [],
      search_terms: '',
      date_range_start: null,
      date_range_end: null,
      assigned_to_id: null,
      assigned_to_username: null,
      due_date: '2026-08-05',
      vault_matter_id: '',
      vault_hold_id: '',
      vault_export_id: '',
      created_at: '2026-07-31T12:00:00Z',
      completed_at: null,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/staff/collection-tasks/11/')) return response(task)
      if (url === '/api/staff/collection-tasks/') {
        return response(page([{
          id: 11,
          request: '11111111-1111-4111-8111-111111111111',
          request_number: 'PRR-2026-0042',
          system_name: 'Google Workspace',
          system_slug: 'google-workspace',
          system_urgent_hold: false,
          kind: 'manual',
          state: 'pending',
          instructions: 'Search the assigned custodians.',
          custodian_emails: [],
          search_terms: '',
          date_range_start: null,
          date_range_end: null,
          assigned_to_id: null,
          assigned_to_username: null,
          due_date: '2026-08-05',
          vault_matter_id: '',
          vault_hold_id: '',
          vault_export_id: '',
          created_at: '2026-07-31T12:00:00Z',
          completed_at: null,
        }]))
      }
      if (url.includes('staff-users') || url.endsWith('/users/')) {
        return response(page([{
          id: 7,
          username: 'case.worker',
          display_name: 'Case Worker',
        }]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    const user = userEvent.setup()
    renderPage(<Tasks roles={['records_officer']} />)
    await user.click(at(await screen.findAllByRole('row'), 1))

    const dialog = screen.getByRole('dialog', { name: /task — request/i })
    const assignee = within(dialog).getByRole('combobox', { name: /assign/i })
    expect(
      within(assignee).getByRole('option', { name: /Case Worker.*case\.worker/i }),
    ).toBeInTheDocument()
    expect(within(dialog).queryByText(/user id/i)).not.toBeInTheDocument()
  })

  it('logs time by public request number instead of requiring an opaque request primary key', async () => {
    let submittedBody: Record<string, unknown> | undefined
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/time-entries/' && method === 'GET') {
        return response(page([]))
      }
      if (url === '/api/staff/time-entries/' && method === 'POST') {
        submittedBody = JSON.parse(String(init?.body)) as Record<string, unknown>
        return response({
          id: 1,
          request: submittedBody.request,
          request_number: 'PRR-2026-0042',
          staff_username: 'case.worker',
          date: submittedBody.date,
          minutes: submittedBody.minutes,
          activity: submittedBody.activity,
        }, 201)
      }
      if (url.startsWith('/api/staff/requests/')) {
        return response(page([{
          id: '22222222-2222-4222-8222-222222222222',
          number: 'PRR-2026-0042',
          status: 'gathering',
        }]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    const user = userEvent.setup()
    renderPage(<TimeEntries roles={['liaison']} />)
    await user.click(await screen.findByRole('button', { name: 'Log time' }))

    const dialog = screen.getByRole('dialog', { name: 'Log time' })
    const request = within(dialog).getByRole('combobox', { name: /^case$/i })
    expect(
      within(request).getByRole('option', { name: 'PRR-2026-0042' }),
    ).toBeInTheDocument()
    expect(within(dialog).queryByText(/numeric id/i)).not.toBeInTheDocument()

    await user.selectOptions(request, '22222222-2222-4222-8222-222222222222')
    fireEvent.change(within(dialog).getByLabelText('Date'), {
      target: { value: '2026-07-31' },
    })
    fireEvent.change(within(dialog).getByLabelText('Minutes'), {
      target: { value: '30' },
    })
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(submittedBody?.request)
        .toBe('22222222-2222-4222-8222-222222222222')
    })
  })

  it('reuses the time-entry mutation inside one case without a request selector', async () => {
    let submittedBody: Record<string, unknown> | undefined
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/time-entries/?request=PRR-2026-0042' && method === 'GET') {
        return response(page([]))
      }
      if (url === '/api/staff/requests/PRR-2026-0042/' && method === 'GET') {
        return response({
          id: '22222222-2222-4222-8222-222222222222',
          number: 'PRR-2026-0042',
        })
      }
      if (url === '/api/staff/time-entries/' && method === 'POST') {
        submittedBody = JSON.parse(String(init?.body)) as Record<string, unknown>
        return response({
          id: 2,
          request: submittedBody.request,
          request_number: 'PRR-2026-0042',
          staff_username: 'case.worker',
          date: submittedBody.date,
          minutes: submittedBody.minutes,
          activity: submittedBody.activity,
        }, 201)
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPage(
      <TimeEntries
        roles={['liaison']}
        requestNumber="PRR-2026-0042"
      />,
    )
    await user.click(await screen.findByRole('button', { name: 'Log time' }))

    const dialog = screen.getByRole('dialog', { name: 'Log time' })
    expect(within(dialog).queryByRole('combobox', { name: /^case$/i }))
      .not.toBeInTheDocument()
    expect(within(dialog).getByText('PRR-2026-0042')).toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText('Date'), {
      target: { value: '2026-08-24' },
    })
    fireEvent.change(within(dialog).getByLabelText('Minutes'), {
      target: { value: '20' },
    })
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(submittedBody).toMatchObject({
      request: '22222222-2222-4222-8222-222222222222',
      date: '2026-08-24',
      minutes: 20,
    }))
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/time-entries/',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('chooses an installment by request number and sequence instead of an installment ID', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0042') {
        return response(page([]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0042') {
        return response(page([]))
      }
      if (url.startsWith('/api/staff/installments/')) {
        return response(page([{
          id: 9,
          request_number: 'PRR-2026-0042',
          sequence: 2,
          fee_amount: '0.00',
          fee_paid_at: null,
          released_at: null,
          download_expires_at: null,
        }]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    const user = userEvent.setup()
    renderPage(
      <CorrespondencePanel
        requestNumber="PRR-2026-0042"
        requesterEmail="requester@example.org"
        roles={['records_officer']}
      />,
    )
    await user.click(await screen.findByRole('button', { name: 'Compose message' }))
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Message type' }),
      'installment_notice',
    )

    const dialog = screen.getByRole('dialog', { name: 'Compose correspondence' })
    const installment = within(dialog).getByRole('combobox', { name: /^installment$/i })
    expect(
      within(installment).getByRole('option', {
        name: /PRR-2026-0042.*Installment 2/i,
      }),
    ).toBeInTheDocument()
    expect(within(dialog).queryByText('Installment ID')).not.toBeInTheDocument()
  })
})
