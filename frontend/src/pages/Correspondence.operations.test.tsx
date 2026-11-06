// ABOUTME: Regression reproductions for Application Admin correspondence operations access.
// ABOUTME: Admins need the allowed operations/readiness view without forbidden case data.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CONFIGURATION_NAV, PRIMARY_NAV, visibleNav } from '../nav'
import { Correspondence } from './Correspondence'

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Correspondence roles={['admin']} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Message operations — Application Admin access', () => {
  it('advertises the read-only correspondence operations screen to an Application Admin', () => {
    const primaryLabels = visibleNav(PRIMARY_NAV, ['admin']).map((item) => item.label)
    const configurationLabels = visibleNav(CONFIGURATION_NAV, ['admin'])
      .map((item) => item.label)

    expect(primaryLabels).toContain('Settings')
    expect(configurationLabels).toContain('Message operations')
  })

  it('loads only operations and readiness without requesting forbidden correspondence data', async () => {
    const requestedPaths: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requestedPaths.push(url)
      if (url === '/api/staff/correspondence-operations/') {
        return response({
          generated_at: '2026-07-31T12:00:00Z',
          counts: {
            queued: 0,
            claimed: 0,
            awaiting_reconciliation: 0,
            retryable: 0,
            failed: 0,
            sent: 0,
            cancelled: 0,
            stuck: 0,
            inbound: 0,
            unmatched: 0,
            quarantined_messages: 0,
            quarantined_attachments: 0,
            delivery_exceptions: 0,
          },
          ages_seconds: {
            oldest_delivery: null,
            oldest_claim: null,
            oldest_stuck: null,
          },
          failure_categories: {},
        })
      }
      if (url === '/api/staff/correspondence-operations/readiness/') {
        return response({
          ready: true,
          checks: [{
            component: 'outbound_transport',
            status: 'ready',
            code: 'enabled',
          }],
        })
      }
      if (
        url.startsWith('/api/staff/correspondence/')
        || url.startsWith('/api/staff/clarification-cycles/')
        || url.startsWith('/api/staff/inbound-correspondence-receipts/')
      ) {
        return response({ detail: 'Forbidden for Application Admin.' }, 403)
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPage()
    expect(await screen.findByText('Oldest delivery: none')).toBeInTheDocument()
    expect(screen.getByText('Delivery readiness')).toBeInTheDocument()

    await waitFor(() => {
      expect(requestedPaths).toContain('/api/staff/correspondence-operations/')
      expect(requestedPaths).toContain('/api/staff/correspondence-operations/readiness/')
    })
    expect(requestedPaths).not.toContain('/api/staff/correspondence/')
    expect(requestedPaths).not.toContain('/api/staff/clarification-cycles/?state=open')
    expect(
      requestedPaths.some((path) => path.startsWith('/api/staff/inbound-correspondence-receipts/')),
    ).toBe(false)
    expect(screen.queryByText('Delivery and conversation queue')).not.toBeInTheDocument()
    expect(screen.queryByText('Open clarification cycles')).not.toBeInTheDocument()
    expect(screen.queryByText('Inbound review')).not.toBeInTheDocument()
  })
})
