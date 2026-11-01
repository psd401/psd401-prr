// ABOUTME: Shared render harness and API fixtures for co-located page tests.
// ABOUTME: Lets a page's regression tests live beside the page instead of in a defect-ID file.
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { ToastProvider } from '../ui'

export function fakeResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

export function page<T>(
  results: T[],
  options: { count?: number; next?: string | null; previous?: string | null } = {},
) {
  return {
    count: options.count ?? results.length,
    next: options.next ?? null,
    previous: options.previous ?? null,
    results,
  }
}

export function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
}

export function renderPage(ui: ReactNode, route = '/') {
  return render(
    <QueryClientProvider client={queryClient()}>
      <ToastProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

export function requestItem(id: number, overrides: Record<string, unknown> = {}) {
  return {
    id,
    number: `PRR-2026-${String(id).padStart(4, '0')}`,
    status: 'received',
    matter_kind: 'public_records',
    matter_kind_display: 'Public records request (RCW 42.56)',
    received_at: '2026-07-01T16:00:00Z',
    statutory_response_due: null,
    ferpa_access_due: null,
    estimated_completion: null,
    closed_at: null,
    requester_email: 'requester@example.org',
    requester_name: 'Example Requester',
    assigned_officer_username: null,
    tolled_business_days: 0,
    acknowledgment_sent_at: null,
    initial_response_delivered: false,
    ...overrides,
  }
}
