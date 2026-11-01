// ABOUTME: Oversight-log UX regression tests for accountable actor labels.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { page as pageOf, renderPage as renderHarness } from '../test/pageHarness'
import { Audit } from './Audit'

function fakeResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as Response
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('shows an accountable username instead of an opaque actor primary key', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({
    count: 1,
    next: null,
    previous: null,
    results: [{
      id: 7,
      at: '2026-07-31T18:00:00Z',
      actor: 42,
      actor_username: 'records.officer',
      action: 'request.status_changed',
      target: 'PRR-2026-0042',
      detail: {},
      ip_address: null,
    }],
  })))
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })

  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter><Audit /></MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )

  expect(await screen.findByText('records.officer')).toBeInTheDocument()
  expect(screen.queryByText('42')).not.toBeInTheDocument()
})

it('renders the first audit page after one request and fetches the next page only on demand', async () => {
  const nextPage = 'http://localhost/api/staff/audit-log/?page=2'
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/staff/audit-log/') {
      return fakeResponse({
        count: 2,
        next: nextPage,
        previous: null,
        results: [{
          id: 1,
          at: '2026-07-31T18:00:00Z',
          actor: null,
          actor_username: null,
          action: 'request.created',
          target: 'PRR-2026-0001',
          detail: {},
          ip_address: null,
        }],
      })
    }
    if (path === nextPage) {
      return fakeResponse({
        count: 2,
        next: null,
        previous: '/api/staff/audit-log/',
        results: [{
          id: 2,
          at: '2026-07-31T17:00:00Z',
          actor: null,
          actor_username: null,
          action: 'request.status_changed',
          target: 'PRR-2026-0002',
          detail: {},
          ip_address: null,
        }],
      })
    }
    throw new Error(`Unexpected request: ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  const user = userEvent.setup()

  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter><Audit /></MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )

  expect(await screen.findByText('request.created')).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(screen.queryByText('request.status_changed')).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Load more' }))

  expect(await screen.findByText('request.status_changed')).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
})

it('explains when the audit filters have no matching entries', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(pageOf([]))))
  renderHarness(<Audit />)

  expect(
    await screen.findByText('No audit entries match the current filters.'),
  ).toBeInTheDocument()
})
