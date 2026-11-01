// ABOUTME: Correspondence configuration coverage.
// ABOUTME: Verifies role-safe transport summaries and the officer operational queue.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import { Correspondence } from './Correspondence'

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as Response
}

function renderPage(
  roles: StaffRole[] = ['records_officer'],
  route = '/configuration/correspondence',
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Correspondence roles={roles} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Correspondence operations page', () => {
  const operations = {
    generated_at: '2026-07-24T12:00:00Z',
    counts: {
      queued: 2,
      claimed: 1,
      awaiting_reconciliation: 1,
      retryable: 1,
      failed: 1,
      sent: 4,
      cancelled: 0,
      stuck: 2,
      inbound: 3,
      unmatched: 1,
      quarantined_messages: 1,
      quarantined_attachments: 2,
      delivery_exceptions: 1,
    },
    ages_seconds: {
      oldest_delivery: 7200,
      oldest_claim: 300,
      oldest_stuck: 5400,
    },
    failure_categories: { provider_unavailable: 1 },
  }
  const readiness = {
    ready: false,
    checks: [{
      component: 'object_storage',
      status: 'not_ready',
      code: 'unavailable',
    }],
  }

  it('keeps Application Admin on PII-free transport operations', async () => {
    const requestedPaths: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requestedPaths.push(url)
      if (url === '/api/staff/correspondence-operations/') return response(operations)
      if (url === '/api/staff/correspondence-operations/readiness/') return response(readiness)
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPage(['admin'], '/configuration/correspondence?focus=failed')

    expect(await screen.findByText('Oldest delivery: 2h')).toBeInTheDocument()
    await waitFor(() => expect(requestedPaths).toHaveLength(2))
    expect(requestedPaths.some((path) => path.startsWith('/api/staff/correspondence/')))
      .toBe(false)
    expect(screen.queryByText('Transport queue')).not.toBeInTheDocument()
  })

  it('restores a focused failed-transport queue for Records Officers', async () => {
    const requestedPaths: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requestedPaths.push(url)
      if (url === '/api/staff/correspondence-operations/') return response(operations)
      if (url === '/api/staff/correspondence-operations/readiness/') return response(readiness)
      if (url === '/api/staff/correspondence/?status=failed') {
        return response({
          count: 1,
          next: null,
          previous: null,
          results: [{
            id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            request_number: 'PRR-2026-0042',
            direction: 'outbound',
            kind: 'general',
            status: 'failed',
            failure_category: 'provider_unavailable',
            created_at: '2026-07-24T10:00:00Z',
            approved_at: '2026-07-24T10:00:00Z',
            queued_at: '2026-07-24T10:01:00Z',
            sent_at: null,
            received_at: null,
            matched_at: null,
            failed_at: '2026-07-24T10:02:00Z',
            cancelled_at: null,
            delivery_status: 'failed',
          }],
        })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPage(['records_officer'], '/configuration/correspondence?focus=failed')

    expect(await screen.findByText('Transport queue')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Failed' }))
      .toHaveAttribute('aria-pressed', 'true')
    expect(await screen.findByText('PRR-2026-0042')).toBeInTheDocument()
    expect(screen.getAllByText('provider_unavailable').length).toBeGreaterThan(0)
    expect(requestedPaths).toContain('/api/staff/correspondence/?status=failed')
  })

  it('keeps the transport queue bounded to one 50-row page', async () => {
    const rows = (start: number) => Array.from({ length: 50 }, (_, offset) => ({
      id: `00000000-0000-4000-8000-${String(start + offset).padStart(12, '0')}`,
      request_number: `PRR-2026-${String(start + offset).padStart(4, '0')}`,
      direction: 'outbound',
      kind: 'general',
      status: 'draft',
      failure_category: '',
      created_at: '2026-07-24T10:00:00Z',
      approved_at: null,
      queued_at: null,
      sent_at: null,
      received_at: null,
      matched_at: null,
      failed_at: null,
      cancelled_at: null,
      delivery_status: null,
    }))
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence-operations/') return response(operations)
      if (url === '/api/staff/correspondence-operations/readiness/') return response(readiness)
      if (url === '/api/staff/correspondence/?direction=outbound') {
        return response({
          count: 100,
          next: 'http://django:8000/api/staff/correspondence/?cursor=older',
          previous: null,
          results: rows(1),
        })
      }
      if (url === '/api/staff/correspondence/?cursor=older') {
        return response({ count: 100, next: null, previous: null, results: rows(51) })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))
    const user = userEvent.setup()

    renderPage()
    expect(await screen.findByText('Showing 1–50 of 100 messages')).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(51)

    await user.click(screen.getByRole('button', { name: 'Load more messages' }))
    expect(await screen.findByText('Showing 51–100 of 100 messages')).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(51)
    expect(screen.queryByText('PRR-2026-0001')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Show newer messages' }))
    expect(await screen.findByText('Showing 1–50 of 100 messages')).toBeInTheDocument()
  })

  it('does not let delayed pagination overwrite a newer transport scope', async () => {
    let resolveOlder!: (value: Response) => void
    const delayedOlder = new Promise<Response>((resolve) => {
      resolveOlder = resolve
    })
    const queueRow = (id: string, status: 'draft' | 'failed') => ({
      id: `00000000-0000-4000-8000-${id.padStart(12, '0')}`,
      request_number: `PRR-2026-${id.padStart(4, '0')}`,
      direction: 'outbound',
      kind: 'general',
      status,
      failure_category: status === 'failed' ? 'provider_unavailable' : '',
      created_at: '2026-07-24T10:00:00Z',
      approved_at: null,
      queued_at: null,
      sent_at: null,
      received_at: null,
      matched_at: null,
      failed_at: null,
      cancelled_at: null,
      delivery_status: status === 'failed' ? 'failed' : null,
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence-operations/') return response(operations)
      if (url === '/api/staff/correspondence-operations/readiness/') return response(readiness)
      if (url === '/api/staff/correspondence/?direction=outbound') {
        return response({
          count: 2,
          next: 'http://django:8000/api/staff/correspondence/?cursor=older',
          previous: null,
          results: [queueRow('1', 'draft')],
        })
      }
      if (url === '/api/staff/correspondence/?cursor=older') return delayedOlder
      if (url === '/api/staff/correspondence/?status=failed') {
        return response({ count: 1, next: null, previous: null, results: [queueRow('9', 'failed')] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))
    const user = userEvent.setup()

    renderPage()
    await screen.findByText('PRR-2026-0001')
    await user.click(screen.getByRole('button', { name: 'Load more messages' }))
    await user.click(screen.getByRole('button', { name: 'Failed' }))
    await screen.findByText('PRR-2026-0009')
    await user.click(screen.getByRole('button', { name: 'All outbound' }))
    await screen.findByText('PRR-2026-0001')

    resolveOlder(response({ count: 2, next: null, previous: null, results: [queueRow('2', 'draft')] }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Load more messages' })).toBeEnabled()
    })

    expect(screen.getByText('PRR-2026-0001')).toBeInTheDocument()
    expect(screen.queryByText('PRR-2026-0002')).not.toBeInTheDocument()
  })
})
