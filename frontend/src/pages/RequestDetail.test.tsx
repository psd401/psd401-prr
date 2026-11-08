// ABOUTME: Tests for RequestDetail — correspondence-only estimate revisions
// ABOUTME: and surfacing a 400 transition-endpoint error inside the status modal.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import type { StaffRole } from '../api/auth'
import {
  renderPage as renderHarness,
  requestItem,
} from '../test/pageHarness'
import { RequestDetail } from './RequestDetail'

const DETAIL = {
  id: 1,
  number: 'PRR-2026-0001',
  status: 'received',
  matter_kind: 'public_records',
  matter_kind_display: 'Public records request (RCW 42.56)',
  received_at: '2026-07-01T12:00:00Z',
  statutory_response_due: '2026-07-10',
  ferpa_access_due: null,
  ferpa_identity_verification: null,
  estimated_completion: '2026-07-15',
  closed_at: null,
  requester_email: 'jane@example.org',
  requester_name: 'Jane Q. Public',
  assigned_officer_username: null,
  tolled_business_days: 0,
  acknowledgment_sent_at: null,
  initial_response_delivered: false,
  initial_response_plan: {
    state: 'scheduled',
    due_date: '2026-07-10',
    automatic_check: 'Hourly at :30 Pacific on and after the due date.',
    correspondence_id: null,
    correspondence_status: null,
    delivery_status: null,
    automated: null,
    next_action: 'Prepare and send the acknowledgment.',
    steps: [
      { key: 'prepare', label: 'Prepare the acknowledgment draft', state: 'current' },
      { key: 'approve', label: 'Records Officer approves the exact wording', state: 'pending' },
      { key: 'queue', label: 'Queue it for delivery', state: 'pending' },
      { key: 'deliver', label: 'Mail provider confirms delivery', state: 'pending' },
    ],
  },
  description: 'All emails about the bond levy.',
  channel: 'email',
  requester: {
    name: 'Jane Q. Public',
    email: 'jane@example.org',
    phone: '555-1212',
    mailing_address: '14015 62nd Ave NW',
    city: 'Gig Harbor',
    state: 'WA',
    postal_code: '98332',
    company: 'Example Organization',
  },
  events: [
    {
      occurred_at: '2026-07-01T12:00:00Z',
      from_status: '',
      to_status: 'received',
      note: 'Intake received.',
      actor: null,
      visible_to_requester: true,
    },
  ],
  estimate_revisions: [],
  collection_tasks: [],
  footage_preservations: [],
  intake_attachments: [],
  email_intake_auto_created: false,
  email_intake_receipt_id: null,
}

function fakeResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function renderPage(
  roles: StaffRole[] = ['records_officer'],
  mode: 'overview' | 'scope' | 'collection' | 'close' = 'overview',
  entry = '/requests/PRR-2026-0001',
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const rendered = render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route
              path="/requests/:number"
              element={<RequestDetail roles={roles} mode={mode} />}
            />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient: qc }
}

// The advisory and Vault-scoping surfaces live on the Scope stage (#45).
function renderScope(roles: StaffRole[] = ['records_officer']) {
  return renderPage(roles, 'scope')
}

// A Scope stage with no advisory proposal and no recorded Vault count,
// where queueing a count is accepted. The end-date tests only differ in what
// the officer does with the form.
function vaultCountFetchMock(countId: string) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
    if (url.endsWith('/advisory-intent/')) {
      return fakeResponse({ enabled: true, latest: null })
    }
    if (url.endsWith('/vault-counts/')) {
      if (init?.method === 'POST') return fakeResponse({ id: countId, state: 'pending' }, 202)
      return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
    }
    return fakeResponse({ count: 0, next: null, previous: null, results: [] })
  })
}

beforeEach(() => {
  document.cookie = 'csrftoken=test-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('RequestDetail', () => {
  it.each(['liaison', 'reviewer', 'admin'] as StaffRole[])(
    'does not invent hidden lifecycle provenance for %s',
    async (role) => {
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/activity/')) {
          // What the server sends a non-officer: the milestone, with the
          // actor and the note withheld rather than nulled into "system".
          return fakeResponse({ results: [{
            id: 'lifecycle:1',
            type: 'lifecycle',
            occurred_at: '2026-07-03T12:00:00Z',
            label: 'Received → Gathering Records',
            actor_username: null,
            detail: null,
            work_item_id: null,
            delivery_status: null,
            flagged: false,
          }] })
        }
        return fakeResponse({
          ...DETAIL,
          requester: null,
          requester_email: null,
          requester_name: null,
          estimate_revisions: [{
            previous_date: '2026-08-01',
            new_date: '2026-08-15',
            reason: '',
            revised_by: null,
            automated: false,
            created_at: '2026-07-03T12:00:00Z',
          }],
        })
      }))

      renderPage([role])

      await screen.findByText('Received → Gathering Records')
      // A hidden actor is not the system acting. Printing "By system" here
      // would manufacture provenance this role was never shown.
      expect(screen.queryByText('By system')).not.toBeInTheDocument()
      expect(screen.queryByText(/unknown/i)).not.toBeInTheDocument()
    },
  )

  it('folds a long request to its opening and shows all of it on request', async () => {
    const thread = Array.from({ length: 30 }, (_, line) => `Quoted line ${line + 1}`).join('\n')
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({ ...DETAIL, description: thread })
      }
      if (url.endsWith('/advisory-intent/')) return fakeResponse({ enabled: true, latest: null })
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))
    const user = userEvent.setup()
    renderPage()

    const toggle = await screen.findByRole('button', { name: 'Show full request' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // The whole text is always in the page, unaltered; folding only clips it.
    expect(screen.getByText(/Quoted line 30/)).toBeInTheDocument()
    await user.click(toggle)
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('shows a short request whole, with nothing to unfold', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) return fakeResponse({ enabled: true, latest: null })
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))
    renderPage()

    expect(await screen.findByText('All emails about the bond levy.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show full request' })).not.toBeInTheDocument()
  })

  it('explains the five-day safety-net schedule on the case itself', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) return fakeResponse({ enabled: true, latest: null })
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))

    renderPage()

    expect(await screen.findByText('Safety net armed')).toBeInTheDocument()
    expect(screen.getByText('Hourly at :30 Pacific on and after the due date.'))
      .toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Review five-day response template' }))
      .toHaveAttribute('href', '/configuration/message-templates')
  })

  it('guides the officer through the ordered statutory send steps', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({
          ...DETAIL,
          initial_response_plan: {
            ...DETAIL.initial_response_plan,
            state: 'approved_not_queued',
            next_action: (
              'Queue the approved acknowledgment for delivery. Approval '
              + 'alone does not satisfy the deadline.'
            ),
            steps: [
              { key: 'prepare', label: 'Prepare the acknowledgment draft', state: 'done' },
              { key: 'approve', label: 'Records Officer approves the exact wording', state: 'done' },
              { key: 'queue', label: 'Queue it for delivery', state: 'current' },
              { key: 'deliver', label: 'Mail provider confirms delivery', state: 'pending' },
            ],
          },
        })
      }
      if (url.endsWith('/advisory-intent/')) return fakeResponse({ enabled: true, latest: null })
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))

    renderPage()

    expect(await screen.findByText(/^Next: Queue the approved acknowledgment/))
      .toBeInTheDocument()
    const steps = screen.getByRole('list', { name: 'Statutory response steps' })
    expect(steps).toBeInTheDocument()
    expect(steps.textContent).toContain('Prepare the acknowledgment draft (complete)')
    expect(steps.textContent).toContain('Queue it for delivery (do this next)')
    expect(steps.textContent).toContain('Mail provider confirms delivery (not started)')
  })

  it('explains manual-only mode and requires a human acknowledgment', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({
          ...DETAIL,
          initial_response_plan: {
            ...DETAIL.initial_response_plan,
            state: 'manual_required',
            automatic_check: (
              'Automatic requester correspondence is disabled; prepare and '
              + 'send a human-approved acknowledgment.'
            ),
          },
        })
      }
      if (url.endsWith('/advisory-intent/')) return fakeResponse({ enabled: true, latest: null })
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))

    renderPage()

    expect(await screen.findByText('Human acknowledgment required'))
      .toBeInTheDocument()
    expect(screen.getByText(/Automatic requester correspondence is disabled/))
      .toBeInTheDocument()
  })

  it('identifies an existing automatic acknowledgment as held', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({
          ...DETAIL,
          initial_response_plan: {
            ...DETAIL.initial_response_plan,
            state: 'automatic_blocked',
            correspondence_id: 'held-acknowledgment',
            correspondence_status: 'queued',
            delivery_status: 'pending',
            automated: true,
          },
        })
      }
      if (url.endsWith('/advisory-intent/')) return fakeResponse({ enabled: true, latest: null })
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))

    renderPage()

    expect(await screen.findByText('Automatic acknowledgment held'))
      .toBeInTheDocument()
    expect(screen.getByText(/Cancel the unsent automatic message/))
      .toBeInTheDocument()
  })

  it('queues and labels request-intent analysis as advisory only', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) {
        if (init?.method === 'POST') {
          return fakeResponse({
            id: 'run-1', state: 'pending', model_identifier: 'approved-model-v1',
            input_sha256: 'e'.repeat(64), input_truncated: false, applied: false,
            proposal: {}, failure_detail: '',
          }, 202)
        }
        return fakeResponse({ enabled: true, latest: null })
      }
      if (url.startsWith('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderScope()
    expect(await screen.findByText(/permanently not applied/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Run advisory' }))

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/advisory-intent/',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('uses editable advisor terms to queue a count-only Vault scope check', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({
          enabled: true,
          latest: {
            id: 'advisory-1', state: 'complete', model_identifier: 'approved-model-v1',
            input_sha256: 'e'.repeat(64), input_truncated: false, applied: false,
            proposal: {
              required_search_terms: ['Jordan Sample'],
              search_terms: ['educational records', 'counseling records', 'jordan sample'],
              record_system_slugs: ['google-vault'],
            },
            failure_detail: '',
          },
        })
      }
      if (url.endsWith('/vault-counts/')) {
        if (init?.method === 'POST') {
          return fakeResponse({
            id: 'count-1', state: 'pending', corpus: 'MAIL',
            terms: '"field project" OR "general contractor"',
            custodian_emails: ['superintendent@psd401.net'],
            date_range_start: '2026-01-01', date_range_end: '2026-06-30',
            input_sha256: 'f'.repeat(64), requested_by_username: 'officer',
            total_count: null, queried_accounts_count: null,
            matching_accounts_count: null, non_queryable_accounts_count: null,
            accounts_with_errors_count: null, failure_code: '', failure_detail: '',
            created_at: '2026-08-10T12:00:00Z', started_at: null, completed_at: null,
          }, 202)
        }
        return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      }
      if (url.startsWith('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderScope()
    const query = await screen.findByLabelText('Vault search query')
    expect(query).toHaveValue(
      '"Jordan Sample" AND ("educational records" OR "counseling records")',
    )
    await user.type(screen.getByLabelText('Custodian accounts'), 'superintendent@psd401.net')
    await user.type(screen.getByLabelText('Start date'), '2026-01-01')
    await user.clear(screen.getByLabelText('End date'))
    await user.type(screen.getByLabelText('End date'), '2026-06-30')
    await user.click(screen.getByRole('button', { name: 'Count possible messages' }))

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/vault-counts/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          terms: '"Jordan Sample" AND ("educational records" OR "counseling records")',
          search_scope: 'accounts',
          custodian_emails: ['superintendent@psd401.net'],
          date_range_start: '2026-01-01',
          date_range_end: '2026-06-30',
        }),
      }),
    )
    expect(screen.getByText(/does not create an export, hold, collection task/i)).toBeInTheDocument()
  })

  it('fills the Vault dates and accounts from the AI reading of the request', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({
          enabled: true,
          latest: {
            id: 'advisory-2', state: 'complete', model_identifier: 'approved-model-v1',
            input_sha256: 'e'.repeat(64), input_truncated: false, applied: false,
            proposal: {
              required_search_terms: ['bus route'],
              search_terms: [],
              custodians: ['transport@psd401.net', 'principal@psd401.net'],
              // An end past receipt is held to the receipt date.
              date_range: { start: '2026-01-01', end: '2026-12-31' },
            },
            failure_detail: '',
          },
        })
      }
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)

    renderScope()

    await waitFor(() => expect(screen.getByLabelText('Start date')).toHaveValue('2026-01-01'))
    expect(screen.getByLabelText('End date')).toHaveValue('2026-07-01')
    expect(screen.getByLabelText('Custodian accounts')).toHaveValue(
      'transport@psd401.net\nprincipal@psd401.net',
    )
  })

  it('uses an AI end date that falls before the receipt date', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({
          enabled: true,
          latest: {
            id: 'advisory-3', state: 'complete', model_identifier: 'approved-model-v1',
            input_sha256: 'e'.repeat(64), input_truncated: false, applied: false,
            proposal: { required_search_terms: ['bus route'], search_terms: [],
              date_range: { start: null, end: '2026-03-31' } },
            failure_detail: '',
          },
        })
      }
      if (url.endsWith('/vault-counts/')) return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)

    renderScope()

    await waitFor(() => expect(screen.getByLabelText('End date')).toHaveValue('2026-03-31'))
    expect(screen.getByLabelText('Start date')).toHaveValue('')
  })

  it('defaults the Vault end date to the receipt date and sends it unedited', async () => {
    const fetchMock = vaultCountFetchMock('count-2')
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderScope()

    // Responsive records are those in existence when the request arrived, so
    // the officer should not have to retype the case's own receipt date.
    const endDate = await screen.findByLabelText('End date')
    expect(endDate).toHaveValue('2026-07-01')

    await user.type(screen.getByLabelText('Vault search query'), 'contract')
    await user.type(screen.getByLabelText('Custodian accounts'), 'superintendent@psd401.net')
    await user.click(screen.getByRole('button', { name: 'Count possible messages' }))

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/vault-counts/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          terms: 'contract',
          search_scope: 'accounts',
          custodian_emails: ['superintendent@psd401.net'],
          date_range_start: null,
          date_range_end: '2026-07-01',
        }),
      }),
    )
  })

  it('keeps an officer-cleared Vault end date empty', async () => {
    const fetchMock = vaultCountFetchMock('count-3')
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderScope()

    // The default is a starting point, not a floor: clearing it must stick
    // rather than be refilled by the effect that supplied it.
    await user.clear(await screen.findByLabelText('End date'))
    await user.type(screen.getByLabelText('Vault search query'), 'contract')
    await user.type(screen.getByLabelText('Custodian accounts'), 'superintendent@psd401.net')
    await user.click(screen.getByRole('button', { name: 'Count possible messages' }))

    expect(screen.getByLabelText('End date')).toHaveValue('')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/vault-counts/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          terms: 'contract',
          search_scope: 'accounts',
          custodian_emails: ['superintendent@psd401.net'],
          date_range_start: null,
          date_range_end: null,
        }),
      }),
    )
  })

  it('allows an explicit all-district-accounts Gmail count without custodians', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({
          enabled: true,
          latest: {
            id: 'advisory-org', state: 'complete', model_identifier: 'approved-model-v1',
            input_sha256: 'e'.repeat(64), input_truncated: false, applied: false,
            proposal: {
              required_search_terms: ['Jordan Sample'],
              search_terms: ['educational records', 'counseling records'],
              record_system_slugs: ['google-vault'],
            },
            failure_detail: '',
          },
        })
      }
      if (url.endsWith('/vault-counts/')) {
        if (init?.method === 'POST') {
          return fakeResponse({
            id: 'count-org', state: 'pending', corpus: 'MAIL',
            search_scope: 'entire_org',
            terms: '"Jordan Sample" AND ("educational records" OR "counseling records")',
            custodian_emails: [], date_range_start: null, date_range_end: null,
            input_sha256: 'f'.repeat(64), requested_by_username: 'officer',
            total_count: null, queried_accounts_count: null,
            matching_accounts_count: null, non_queryable_accounts_count: null,
            accounts_with_errors_count: null, failure_code: '', failure_detail: '',
            created_at: '2026-08-11T12:00:00Z', started_at: null, completed_at: null,
          }, 202)
        }
        return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderScope()
    await screen.findByLabelText('Vault search query')
    await user.click(screen.getByRole('radio', { name: 'All district accounts' }))
    const button = screen.getByRole('button', { name: 'Count possible messages' })
    expect(button).toBeEnabled()
    await user.click(button)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/vault-counts/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          terms: '"Jordan Sample" AND ("educational records" OR "counseling records")',
          search_scope: 'entire_org',
          custodian_emails: [],
          date_range_start: null,
          date_range_end: '2026-07-01',
        }),
      }),
    )
  })

  it('labels a completed Vault result as possible Gmail messages', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/vault-counts/')) {
        return fakeResponse({
          enabled: true, corpus: 'MAIL', latest: {
            id: 'count-1', state: 'complete', corpus: 'MAIL', terms: 'project',
            custodian_emails: ['superintendent@psd401.net'],
            date_range_start: null, date_range_end: null,
            input_sha256: 'f'.repeat(64), requested_by_username: 'officer',
            total_count: 1842, queried_accounts_count: 2,
            matching_accounts_count: 2, non_queryable_accounts_count: 0,
            accounts_with_errors_count: 0, failure_code: '', failure_detail: '',
            created_at: '2026-08-10T12:00:00Z',
            started_at: '2026-08-10T12:00:01Z', completed_at: '2026-08-10T12:00:10Z',
          },
        })
      }
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({ enabled: true, latest: null })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))

    renderScope()

    expect(await screen.findByText('1,842 possible Gmail messages')).toBeInTheDocument()
    expect(screen.getAllByText(/not a responsiveness determination/i)).toHaveLength(2)
  })

  it('requires explicit confirmation before turning the reviewed count into collection work', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({ enabled: true, latest: null })
      }
      if (url.endsWith('/vault-counts/')) {
        return fakeResponse({
          enabled: true, corpus: 'MAIL', latest: {
            id: 'd16fb086-53d0-4f11-a9da-10a41a580001',
            state: 'complete', corpus: 'MAIL', search_scope: 'entire_org',
            terms: '"Jordan Sample" AND (IEP OR grades)', custodian_emails: [],
            date_range_start: '2026-01-01', date_range_end: '2026-08-01',
            input_sha256: 'f'.repeat(64), requested_by_username: 'officer',
            total_count: 1842, queried_accounts_count: 2500,
            matching_accounts_count: 12, non_queryable_accounts_count: 0,
            accounts_with_errors_count: 0, failure_code: '', failure_detail: '',
            created_at: '2026-08-10T12:00:00Z',
            started_at: '2026-08-10T12:00:01Z', completed_at: '2026-08-10T12:00:10Z',
          },
        })
      }
      if (url.endsWith('/vault-collection/') && init?.method === 'POST') {
        return fakeResponse({
          id: 41, request: 1, request_number: 'PRR-2026-0001',
          system_name: null, system_slug: null, system_urgent_hold: null,
          kind: 'vault_mail', state: 'pending', instructions: 'Review after ingest.',
          custodian_emails: [], search_terms: '"Jordan Sample" AND (IEP OR grades)',
          date_range_start: '2026-01-01', date_range_end: '2026-08-01',
          source_vault_count_id: 'd16fb086-53d0-4f11-a9da-10a41a580001',
          assigned_to_id: null, assigned_to_username: null, due_date: '2026-08-13',
          vault_matter_id: '', vault_hold_id: '', vault_export_id: '',
          vault_ingest: null, created_at: '2026-08-11T12:00:00Z', completed_at: null,
        }, 202)
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderScope()
    await screen.findByText('1,842 possible Gmail messages')
    await user.click(screen.getByRole('button', { name: 'Start Vault collection' }))

    const dialog = screen.getByRole('dialog', { name: 'Confirm Vault collection' })
    expect(within(dialog).getByText(/all district accounts/i)).toBeInTheDocument()
    const submit = within(dialog).getByRole('button', { name: 'Start export' })
    expect(submit).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox', { name: /reviewed this exact query/i }))
    await user.click(submit)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/vault-collection/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          count_run_id: 'd16fb086-53d0-4f11-a9da-10a41a580001',
          expected_input_sha256: 'f'.repeat(64),
          confirmed: true,
        }),
      }),
    )
    expect(await screen.findByText(/Vault export queued/i)).toBeInTheDocument()
  })

  it('offers a clear retry when a Vault worker handoff failed before export', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({ enabled: true, latest: null })
      }
      if (url.endsWith('/vault-counts/')) {
        return fakeResponse({
          enabled: true, corpus: 'MAIL', latest: {
            id: 'd16fb086-53d0-4f11-a9da-10a41a580001',
            state: 'complete', corpus: 'MAIL', search_scope: 'accounts',
            terms: 'project', custodian_emails: ['officer@psd401.net'],
            date_range_start: null, date_range_end: null,
            input_sha256: 'f'.repeat(64), requested_by_username: 'officer',
            collection_task_id: 41, collection_task_state: 'failed',
            collection_task_retryable: true,
            total_count: 42, queried_accounts_count: 1,
            matching_accounts_count: 1, non_queryable_accounts_count: 0,
            accounts_with_errors_count: 0, failure_code: '', failure_detail: '',
            created_at: '2026-08-10T12:00:00Z',
            started_at: '2026-08-10T12:00:01Z', completed_at: '2026-08-10T12:00:10Z',
          },
        })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))

    renderScope()

    expect(await screen.findByText(/failed before a Vault export was recorded/i))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry Vault collection' }))
      .toBeInTheDocument()
  })

  it('keeps legal actions out of a reviewer session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({
      ...DETAIL,
      requester: null,
      intake_attachments: [],
    })))

    renderPage(['reviewer'])

    await screen.findByText('Running log')
    expect(screen.getByText('Restricted')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Change status' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revise estimate' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Messages' })).not.toBeInTheDocument()
  })

  it('shows collected requester details and malware-gated attachments', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({
      ...DETAIL,
      intake_attachments: [
        {
          id: 'attachment-1',
          original_filename: 'context.pdf',
          content_type: 'application/pdf',
          size: 1024,
          sha256: 'a'.repeat(64),
          scan_status: 'pending',
          created_at: '2026-07-01T12:00:00Z',
          scanned_at: null,
        },
      ],
    })))

    renderPage()

    expect(await screen.findByText('Example Organization')).toBeInTheDocument()
    expect(screen.getByText('14015 62nd Ave NW')).toBeInTheDocument()
    expect(screen.getByText('Gig Harbor, WA 98332')).toBeInTheDocument()
    expect(screen.getByText('context.pdf')).toBeInTheDocument()
    expect(screen.getByText('Scanning')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument()
  })

  it('never offers an estimate revision outside requester correspondence', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) return fakeResponse(DETAIL)
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/requests/PRR-2026-0001/activity/')) {
        return fakeResponse({ results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage()

    await screen.findByRole('heading', { name: 'Estimate' })
    // Rule 6: revising an estimate is a human, reasoned act that happens in
    // requester correspondence (the Messages tab). The case detail must never
    // grow a shortcut for it.
    expect(
      screen.queryByRole('button', { name: 'Revise estimate' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { name: 'Messages' }),
    ).not.toBeInTheDocument()
  })

  it('surfaces a 400 error message from the transition endpoint', async () => {
    const user = userEvent.setup()
    const errorMessage =
      'This request is already closed (fulfilled) and cannot be transitioned further.'
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/') && method === 'GET') {
        return fakeResponse(DETAIL)
      }
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/transition/') && method === 'POST') {
        return fakeResponse({ detail: errorMessage }, 400)
      }
      if (url.includes('/activity/')) return fakeResponse({ results: [] })
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage()

    await screen.findByText('Running log')
    await user.click(screen.getByRole('button', { name: 'Change status' }))

    const modalHeading = await screen.findByRole('heading', { name: 'Change status' })
    const dialog = modalHeading.closest('dialog') as HTMLElement

    await user.selectOptions(within(dialog).getByLabelText('New status'), 'gathering')
    await user.click(within(dialog).getByRole('button', { name: 'Change status' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(errorMessage)
  })

  it('refreshes the persistent case summary after a lifecycle transition', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/transition/')) {
        return fakeResponse({ ...DETAIL, status: 'gathering' })
      }
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) {
        return fakeResponse(DETAIL)
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))
    const { queryClient } = renderPage()
    queryClient.setQueryData(
      ['case-workspace-summary', 'PRR-2026-0001'],
      { legal_status: 'received' },
    )

    await screen.findByText('Running log')
    await user.click(screen.getByRole('button', { name: 'Change status' }))
    const dialog = screen.getByRole('heading', { name: 'Change status' })
      .closest('dialog') as HTMLElement
    await user.selectOptions(within(dialog).getByLabelText('New status'), 'gathering')
    await user.click(within(dialog).getByRole('button', { name: 'Change status' }))

    await waitFor(() => expect(
      queryClient.getQueryState(['case-workspace-summary', 'PRR-2026-0001'])
        ?.isInvalidated,
    ).toBe(true))
  })

  it('keeps communication-driven legal states out of the generic status picker', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) {
        return fakeResponse(DETAIL)
      }
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/requests/PRR-2026-0001/activity/')) {
        return fakeResponse({ results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPage()

    await screen.findByText('Running log')
    await user.click(screen.getByRole('button', { name: 'Change status' }))
    const dialog = screen.getByRole('heading', { name: 'Change status' })
      .closest('dialog') as HTMLElement
    const status = within(dialog).getByLabelText('New status')

    expect(within(status).queryByRole('option', { name: 'Acknowledged' })).not.toBeInTheDocument()
    expect(
      within(status).queryByRole('option', { name: 'Awaiting Clarification' }),
    ).not.toBeInTheDocument()
    expect(
      within(status).queryByRole('option', { name: 'Denied (fully exempt)' }),
    ).not.toBeInTheDocument()
    expect(
      within(status).getByRole('option', { name: 'Gathering Records' }),
    ).toBeInTheDocument()
    expect(
      within(status).queryByRole('option', { name: 'Received' }),
    ).not.toBeInTheDocument()
  })

  it('offers one-step substantive closure for an auto-created email non-request', async () => {
    const user = userEvent.setup()
    const reason = 'Automated vendor solicitation; no records were requested.'
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/') && method === 'GET') {
        return fakeResponse({ ...DETAIL, email_intake_auto_created: true })
      }
      if (
        url.endsWith('/api/staff/requests/PRR-2026-0001/close-non-request/')
        && method === 'POST'
      ) {
        return fakeResponse({
          ...DETAIL,
          email_intake_auto_created: true,
          status: 'closed_non_request',
          closed_at: '2026-07-02T12:00:00Z',
        })
      }
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/activity/')) return fakeResponse({ results: [] })
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage()

    await screen.findByText('Running log')
    await user.click(screen.getByRole('button', { name: 'Close as non-request' }))
    const dialog = screen.getByRole('heading', { name: 'Close as non-request' })
      .closest('dialog') as HTMLElement
    const reasonField = within(dialog).getByLabelText(/^Closure reason/)
    const submit = within(dialog).getByRole('button', { name: 'Close as non-request' })
    expect(reasonField).toBeRequired()
    expect(reasonField).toHaveAttribute('minlength', '10')
    expect(submit).toBeDisabled()

    await user.type(reasonField, reason)
    await user.click(submit)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/close-non-request/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ reason }),
      }),
    )
  })

  // The status label itself now lives in the persistent case header rendered by
  // CaseWorkspace, not here. What this file still owns is the rule that a
  // terminal case offers no way to transition out of it.
  it.each([
    'fulfilled',
    'closed_withdrawn',
    'closed_abandoned',
    'closed_non_request',
    'closed_no_records',
    'denied',
  ])('hides transition controls when a request is terminal (%s)', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) {
        return fakeResponse({ ...DETAIL, status })
      }
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/requests/PRR-2026-0001/activity/')) {
        return fakeResponse({ results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPage()

    await screen.findByRole('heading', { name: 'Request' })
    expect(screen.queryByRole('button', { name: 'Change status' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Close request' })).not.toBeInTheDocument()
  })

  it.each([
    ['fulfilled', 'Fulfilled'],
  ])('requires a substantive reason before transitioning to %s', async (toStatus, label) => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) {
        return fakeResponse(DETAIL)
      }
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/requests/PRR-2026-0001/activity/')) {
        return fakeResponse({ results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPage()

    await screen.findByText('Running log')
    await user.click(screen.getByRole('button', { name: 'Change status' }))
    const dialog = screen.getByRole('heading', { name: 'Change status' })
      .closest('dialog') as HTMLElement
    await user.selectOptions(within(dialog).getByLabelText('New status'), toStatus)

    const reason = within(dialog).getByLabelText(/^Reason/)
    const submit = within(dialog).getByRole('button', { name: 'Change status' })
    expect(within(dialog).getByText(/Required — at least 10 characters/)).toBeInTheDocument()
    expect(reason).toBeRequired()
    expect(reason).toHaveAttribute('minlength', '10')
    expect(submit).toBeDisabled()

    await user.type(reason, '123456789')
    expect(submit).toBeDisabled()
    await user.type(reason, '0')
    expect(submit).toBeEnabled()
    expect(within(dialog).getByLabelText('New status')).toHaveValue(toStatus)
    expect(within(dialog).getByRole('option', { name: label })).toBeInTheDocument()
  })

  it('keeps clarification resolution guidance and offers only cancellation closures', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) {
        return fakeResponse({ ...DETAIL, status: 'clarification' })
      }
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/requests/PRR-2026-0001/activity/')) {
        return fakeResponse({ results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPage()

    await screen.findByText('Running log')
    expect(
      screen.queryByRole('button', { name: 'Change status' }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByText(/Resolve the clarification from Messages/),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close case…' }))

    const dialog = screen.getByRole('heading', { name: 'Close case' })
      .closest('dialog') as HTMLElement
    const status = within(dialog).getByLabelText('How this case ended')
    expect(
      within(status).getByRole('option', { name: 'Closed – Withdrawn' }),
    ).toBeInTheDocument()
    expect(
      within(status).getByRole('option', { name: 'Closed – Abandoned' }),
    ).toBeInTheDocument()
    for (const forbidden of [
      'Received',
      'Gathering Records',
      'Review & Redaction',
      'Producing Installments',
      'Fulfilled',
      'Denied (fully exempt)',
    ]) {
      expect(
        within(status).queryByRole('option', { name: forbidden }),
      ).not.toBeInTheDocument()
    }
    expect(within(dialog).getByLabelText(/Closure reason/)).toBeRequired()
    expect(
      within(dialog).getByText(/no response is fabricated for it/),
    ).toBeInTheDocument()
  })
})

describe('RequestDetail — closing a case', () => {
  function stubCase(status = 'gathering') {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/close/')) {
        closeCalls.push(JSON.parse(String(init?.body)))
        return fakeResponse({ ...DETAIL, status: 'closed_withdrawn' })
      }
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) {
        return fakeResponse({ ...DETAIL, status })
      }
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/requests/PRR-2026-0001/activity/')) {
        return fakeResponse({ results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))
  }

  let closeCalls: { to_status: string; reason: string }[] = []

  beforeEach(() => {
    closeCalls = []
  })

  it.each<StaffRole[]>([
    ['records_officer'],
    ['liaison'],
    ['reviewer'],
    ['admin'],
  ])('offers the closure to %s, not only the Records Officer', async (role) => {
    stubCase()

    renderPage([role])

    await screen.findByText('Running log')
    expect(
      screen.getByRole('button', { name: 'Close case…' }),
    ).toBeInTheDocument()
  })

  it('does not offer the working-state control to a role that cannot change it', async () => {
    stubCase()

    renderPage(['liaison'])

    await screen.findByText('Running log')
    expect(
      screen.queryByRole('button', { name: 'Change status' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Close as non-request' }),
    ).not.toBeInTheDocument()
  })

  it('sends the chosen closure and its reason, and refuses a thin one', async () => {
    const user = userEvent.setup()
    stubCase()

    renderPage(['reviewer'])

    await screen.findByText('Running log')
    await user.click(screen.getByRole('button', { name: 'Close case…' }))
    const dialog = screen.getByRole('heading', { name: 'Close case' })
      .closest('dialog') as HTMLElement
    const submit = within(dialog).getByRole('button', { name: 'Close case' })

    expect(submit).toBeDisabled()
    await user.selectOptions(
      within(dialog).getByLabelText('How this case ended'),
      'closed_abandoned',
    )
    expect(submit).toBeDisabled()
    await user.type(within(dialog).getByLabelText(/Closure reason/), 'too short')
    expect(submit).toBeDisabled()

    await user.clear(within(dialog).getByLabelText(/Closure reason/))
    await user.type(
      within(dialog).getByLabelText(/Closure reason/),
      'Requester did not clarify by the documented deadline.',
    )
    expect(submit).toBeEnabled()
    await user.click(submit)

    await waitFor(() => expect(closeCalls).toHaveLength(1))
    expect(closeCalls[0]).toEqual({
      to_status: 'closed_abandoned',
      reason: 'Requester did not clarify by the documented deadline.',
    })
  })

  it('offers no closure at all on an already closed case', async () => {
    stubCase('closed_withdrawn')

    renderPage(['liaison'])

    await screen.findByText('Running log')
    expect(
      screen.queryByRole('button', { name: 'Close case…' }),
    ).not.toBeInTheDocument()
  })
})

describe('RequestDetail — adding an emailed message to an existing case', () => {
  let redirectCalls: Record<string, unknown>[] = []

  function stubAutoCreatedCase({
    receiptId = 'receipt-1',
  }: { receiptId?: string | null } = {}) {
    redirectCalls = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/redirect/')) {
        redirectCalls.push(JSON.parse(String(init?.body)))
        return fakeResponse({ id: receiptId, version: 4 })
      }
      if (url.includes('/api/staff/inbound-correspondence-receipts/')) {
        return fakeResponse({ id: receiptId, version: 3 })
      }
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/')) {
        return fakeResponse({
          ...DETAIL,
          email_intake_auto_created: true,
          email_intake_receipt_id: receiptId,
        })
      }
      if (url.includes('/api/staff/correspondence/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/clarification-cycles/')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url.includes('/api/staff/requests/PRR-2026-0001/activity/')) {
        return fakeResponse({ results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))
  }

  it('moves the message with the receipt version the server checks', async () => {
    const user = userEvent.setup()
    stubAutoCreatedCase()

    renderPage(['records_officer'])

    await screen.findByText('Running log')
    const trigger = await screen.findByRole('button', {
      name: 'Add to an existing case…',
    })
    await waitFor(() => expect(trigger).toBeEnabled())
    await user.click(trigger)

    const dialog = screen.getByRole('heading', { name: 'Add to an existing case' })
      .closest('dialog') as HTMLElement
    await user.type(
      within(dialog).getByLabelText(/Case to add this message to/),
      'PRR-2026-0042',
    )
    await user.type(
      within(dialog).getByLabelText(/Why this message belongs to that case/),
      'Follow-up to the open stadium correspondence request.',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Add to case' }))

    await waitFor(() => expect(redirectCalls).toHaveLength(1))
    expect(redirectCalls[0]).toEqual({
      request_number: 'PRR-2026-0042',
      reason: 'Follow-up to the open stadium correspondence request.',
      expected_version: 3,
    })
  })

  it('never offers the move to a role that cannot read the retained message', async () => {
    stubAutoCreatedCase()

    renderPage(['liaison'])

    await screen.findByText('Running log')
    expect(
      screen.queryByRole('button', { name: 'Add to an existing case…' }),
    ).not.toBeInTheDocument()
  })

  it('cannot be started when no retained receipt backs the case', async () => {
    stubAutoCreatedCase({ receiptId: null })

    renderPage(['records_officer'])

    await screen.findByText('Running log')
    await waitFor(() => expect(
      screen.getByRole('button', { name: 'Add to an existing case…' }),
    ).toBeDisabled())
  })
})

describe('RequestDetail — timeline labels', () => {
  it('labels a note-only status event as an operational note instead of a transition', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/activity/')) {
        return fakeResponse({ results: [{
          id: 'lifecycle:9',
          type: 'lifecycle',
          occurred_at: '2026-07-02T12:00:00Z',
          label: 'Operational note',
          actor_username: null,
          detail: null,
          work_item_id: null,
          delivery_status: null,
          flagged: false,
        }] })
      }
      return fakeResponse({
        ...requestItem(1),
        description: 'Camera footage from the high school entrance.',
        channel: 'email',
        requester: null,
        events: [],
        estimate_revisions: [],
        collection_tasks: [],
        footage_preservations: [],
        intake_attachments: [],
      })
    }))

    renderHarness(
      <Routes>
        <Route
          path="/requests/:number"
          element={<RequestDetail roles={['reviewer']} mode="overview" />}
        />
      </Routes>,
      '/requests/PRR-2026-0001',
    )

    expect(await screen.findByText('Operational note')).toBeInTheDocument()
    expect(screen.queryByText(/New request/)).not.toBeInTheDocument()
  })
})

describe('merging a duplicate case', () => {
  it('merges into the case of record and moves the officer there', async () => {
    const user = userEvent.setup()
    const target = {
      ...DETAIL,
      id: 48,
      number: 'PRR-2026-0048',
      merged_sources: ['PRR-2026-0001'],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/') && method === 'GET') {
        return fakeResponse(DETAIL)
      }
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/merge/') && method === 'POST') {
        return fakeResponse(target)
      }
      if (url.endsWith('/api/staff/requests/PRR-2026-0048/') && method === 'GET') {
        return fakeResponse(target)
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage()

    await screen.findByText('Running log')
    await user.click(screen.getByRole('button', { name: 'Merge into case…' }))
    const heading = await screen.findByRole('heading', { name: 'Merge into case' })
    const dialog = heading.closest('dialog') as HTMLElement
    // Officers type case numbers the way they say them; the request sends
    // the number the way the system spells it.
    await user.type(within(dialog).getByLabelText(/^Merge into case number/), 'prr-2026-0048')
    await user.type(
      within(dialog).getByLabelText(/^Reason/),
      'Same requester; the web form arrived first.',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Merge into case' }))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/merge/'))).toBe(true),
    )
    const mergeCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/merge/'))
    expect(JSON.parse(String(mergeCall?.[1]?.body))).toEqual({
      target_number: 'PRR-2026-0048',
      reason: 'Same requester; the web form arrived first.',
    })
    // The officer lands on the case of record, which says what it absorbed.
    expect(await screen.findByText(/Includes merged case/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'PRR-2026-0001' }))
      .toHaveAttribute('href', '/requests/PRR-2026-0001')
  })

  it('shows where a merged duplicate went and offers nothing else', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({
      ...DETAIL,
      status: 'closed_duplicate',
      closed_at: '2026-09-09T18:00:00Z',
      merged_into: 'PRR-2026-0048',
      initial_response_plan: {
        ...DETAIL.initial_response_plan,
        state: 'merged',
        next_action: '',
        steps: [],
      },
    })))

    renderPage()

    await screen.findByText('Running log')
    expect(screen.getByRole('link', { name: 'PRR-2026-0048' }))
      .toHaveAttribute('href', '/requests/PRR-2026-0048')
    expect(screen.getByText('Merged into the case of record')).toBeInTheDocument()
    // Reopening would leave an empty case with a retired number.
    expect(screen.queryByRole('button', { name: 'Reopen request' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Change status' })).not.toBeInTheDocument()
  })
})

describe('a matter governed by other law', () => {
  const note = 'Washington driver licence checked against the guardian of record.'
  const ferpa = {
    ...DETAIL,
    number: 'FER-2026-0001',
    matter_kind: 'ferpa_parent',
    matter_kind_display: 'FERPA parent or eligible student (34 CFR 99.10)',
    statutory_response_due: null,
    ferpa_access_due: '2026-08-15',
    initial_response_plan: {
      ...DETAIL.initial_response_plan,
      state: 'not_applicable',
      due_date: null,
      next_action:
        'This matter is not a public records request and owes no RCW '
        + '42.56.520 five-day response.',
    },
  }
  const verified = {
    ...ferpa,
    ferpa_identity_verification: {
      verified_at: '2026-07-02T18:00:00Z',
      verified_by: 'officer',
      method: 'in_person_photo_id',
      method_display: 'In-person photo identification',
      note,
    },
  }

  function renderFerpa(detail: unknown, onVerify?: () => void) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/ferpa-identity-verification/') && method === 'POST') {
        onVerify?.()
        return fakeResponse(verified)
      }
      if (url.includes('/api/staff/requests/') && method === 'GET') {
        return fakeResponse(detail)
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPage()
    return fetchMock
  }

  it('shows the FERPA clock, not the RCW one, and asks for identity first', async () => {
    renderFerpa(ferpa)

    await screen.findByText('Running log')
    expect(screen.getByText('FERPA parent or eligible student (34 CFR 99.10)'))
      .toBeInTheDocument()
    // The 45-day FERPA date is shown; the five-day RCW row is not there at all.
    expect(screen.getByText('FERPA access due (45 days)')).toBeInTheDocument()
    expect(screen.getByText('Aug 15, 2026')).toBeInTheDocument()
    expect(screen.queryByText('Statutory response due')).not.toBeInTheDocument()
    expect(screen.queryByText('Tolled business days')).not.toBeInTheDocument()
    // The safety net never sees this matter, so the card must not claim it
    // is waiting to arm.
    expect(screen.getByText('No RCW 42.56.520 response owed')).toBeInTheDocument()
    expect(screen.queryByText('Deadline calculation pending')).not.toBeInTheDocument()
    // Nothing may be disclosed until a named officer has verified identity,
    // and the PRA-only attestation action is not offered here.
    expect(screen.getByText(/Nothing may be disclosed to this requester/))
      .toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Record a response sent outside this system…' }),
    ).not.toBeInTheDocument()
  })

  it('records the identity verification under the officer name', async () => {
    const user = userEvent.setup()
    let recorded = false
    const fetchMock = renderFerpa(ferpa, () => {
      recorded = true
    })

    await screen.findByText('Running log')
    await user.click(
      screen.getByRole('button', { name: 'Record identity verification…' }),
    )
    const heading = await screen.findByRole('heading', {
      name: 'Record FERPA identity verification',
    })
    const dialog = heading.closest('dialog') as HTMLElement
    await user.type(within(dialog).getByLabelText(/^What you relied on/), note)
    await user.click(
      within(dialog).getByRole('button', { name: 'Record verification' }),
    )

    await waitFor(() => expect(recorded).toBe(true))
    const call = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith('/ferpa-identity-verification/'))
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      method: 'in_person_photo_id',
      note,
    })
  })

  it('shows the recorded verification instead of the action', async () => {
    renderFerpa(verified)

    await screen.findByText('Running log')
    expect(
      await screen.findByText(
        /Identity verified under 34 CFR 99.10 by in-person photo identification — officer/,
      ),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Record identity verification…' }),
    ).not.toBeInTheDocument()
  })
})

describe('a first response sent outside this system', () => {
  const evidence = 'Acknowledged in NextRequest before the cutover; copy in the officer mailbox.'
  const attested = {
    ...DETAIL,
    initial_response_delivered: true,
    initial_response_plan: { ...DETAIL.initial_response_plan, state: 'delivered', next_action: '' },
    external_initial_response: {
      sent_via: 'nextrequest',
      sent_via_label: 'NextRequest (retired system)',
      responded_on: null,
      evidence,
      attested_by: 'officer',
      created_at: '2026-09-09T18:00:00Z',
    },
  }

  it('records the officer statement and shows it on the case', async () => {
    const user = userEvent.setup()
    let recorded = false
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/external-initial-response/') && method === 'POST') {
        recorded = true
        return fakeResponse(attested)
      }
      if (url.endsWith('/api/staff/requests/PRR-2026-0001/') && method === 'GET') {
        return fakeResponse(recorded ? attested : DETAIL)
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage()

    await screen.findByText('Running log')
    await user.click(
      screen.getByRole('button', { name: 'Record a response sent outside this system…' }),
    )
    const heading = await screen.findByRole('heading', {
      name: 'Record a response sent outside this system',
    })
    const dialog = heading.closest('dialog') as HTMLElement
    await user.type(within(dialog).getByLabelText(/^What was sent/), evidence)
    await user.click(within(dialog).getByRole('button', { name: 'Record response' }))

    await waitFor(() => expect(recorded).toBe(true))
    const call = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith('/external-initial-response/'))
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      sent_via: 'nextrequest',
      responded_on: null,
      evidence,
    })
    // The statement is on the case, and the action is gone: it was made.
    expect(await screen.findByText(
      /Recorded as sent through NextRequest \(retired system\), date not on record by officer/,
    )).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Record a response sent outside this system…' }),
    ).not.toBeInTheDocument()
  })

  it('offers the action to no one but a Records Officer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({ ...DETAIL, requester: null })))

    renderPage(['liaison'])

    await screen.findByText('Running log')
    expect(
      screen.queryByRole('button', { name: 'Record a response sent outside this system…' }),
    ).not.toBeInTheDocument()
  })
  it('a confirmed duplicate label opens the merge dialog prefilled', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/advisory-intake/feedback/')) {
        expect(init?.method).toBe('POST')
        return fakeResponse({ outcome: 'confirmed' }, 201)
      }
      if (url.endsWith('/advisory-intake/')) {
        return fakeResponse({
          enabled: true,
          latest: {
            id: 'run-1',
            state: 'complete',
            model_identifier: 'us.anthropic.claude-opus-5-5',
            proposal: {
              label: 'duplicate_of',
              target_case_number: 'PRR-2026-0048',
              confidence: 0.9,
              evidence: 'Same wording.',
            },
            failure_detail: '',
          },
          feedback: null,
        })
      }
      return fakeResponse(DETAIL)
    }))
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Correct' }))

    const heading = await screen.findByRole('heading', { name: 'Merge into case' })
    const dialog = heading.closest('dialog') as HTMLElement
    expect(within(dialog).getByLabelText(/^Merge into case number/)).toHaveValue('PRR-2026-0048')
    expect(
      (within(dialog).getByLabelText(/^Reason/) as HTMLTextAreaElement).value,
    ).toContain('duplicate of PRR-2026-0048')
  })

  it('opens the prefilled action when Correct was pressed on the Inbox row', async () => {
    // #33: Inbox Correct already recorded agreement; the case opens the same
    // prefilled dialog, and the officer still reads and submits it.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/advisory-intake/')) {
        return fakeResponse({ enabled: true, latest: null, feedback: null, auto_applied: null })
      }
      return fakeResponse(DETAIL)
    }))
    renderPage(
      ['records_officer'],
      'overview',
      '/requests/PRR-2026-0001?intake_action=duplicate_of&intake_target=PRR-2026-0048',
    )

    const heading = await screen.findByRole('heading', { name: 'Merge into case' })
    const dialog = heading.closest('dialog') as HTMLElement
    expect(within(dialog).getByLabelText(/^Merge into case number/)).toHaveValue('PRR-2026-0048')
  })

  it('ignores an Inbox action for someone who is not a Records Officer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({ ...DETAIL, requester: null })))
    renderPage(['liaison'], 'overview', '/requests/PRR-2026-0001?intake_action=duplicate_of&intake_target=PRR-2026-0048')
    await screen.findByText('Running log')
    expect(screen.queryByRole('heading', { name: 'Merge into case' })).not.toBeInTheDocument()
  })
})
