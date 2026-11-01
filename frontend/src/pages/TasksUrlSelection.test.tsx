// ABOUTME: The collection-task dialog follows the ?task= URL parameter, so a
// ABOUTME: selection the router never committed (e.g. Back raced it) stays closed.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { Tasks } from './Tasks'
import { at } from '../test/at'

const setSearchParams = vi.fn()

vi.mock('react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router')>()
  // The URL never gains ?task=, standing in for a router update that Back
  // superseded before it committed.
  return { ...actual, useSearchParams: () => [new URLSearchParams(), setSearchParams] }
})

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as Response
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  setSearchParams.mockReset()
})

it('does not show a task whose id is not in the URL', async () => {
  const task = {
    id: 23,
    request: '11111111-1111-4111-8111-111111111111',
    request_number: 'PRR-2026-0023',
    system_name: 'Finance',
    system_slug: 'finance',
    system_urgent_hold: false,
    kind: 'manual',
    state: 'pending',
    instructions: 'Search the ledger archive.',
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
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/staff-users/')) return response({ results: [] })
    return response({ results: [task] })
  }))
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const user = userEvent.setup()

  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter><Tasks roles={['records_officer']} /></MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )

  const row = at(await screen.findAllByRole('row'), 1)
  await user.click(row)

  const next = setSearchParams.mock.calls.at(-1)?.[0] as URLSearchParams
  expect(next.get('task')).toBe('23')
  expect(screen.queryByText('Search the ledger archive.')).not.toBeInTheDocument()
  expect(screen.queryByText(/Task — request #PRR-2026-0023/)).not.toBeInTheDocument()
})
