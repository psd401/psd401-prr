// ABOUTME: Routing, role, lazy-load, caching, and focus contracts for the case
// ABOUTME: stage rail (#45); legacy tab URLs redirect without broadening authority.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import { ToastProvider } from '../ui'
import { CaseWorkspace } from './CaseWorkspace'
import { LegacyRequestRedirect } from './LegacyScopedRoutes'

const SUMMARY = {
  number: 'PRR-2026-0001',
  legal_status: 'gathering',
  requester: { name: 'Jane Q. Public', email: 'jane@example.org' },
  owner: { id: 7, username: 'officer', display_name: 'Case Officer' },
  statutory_response: {
    state: 'delivered',
    due_date: '2026-07-10',
    next_action: '',
    correspondence_id: 'ack-1',
    steps: [
      { key: 'prepare', label: 'Prepare the acknowledgment draft', state: 'done' },
      { key: 'approve', label: 'Records Officer approves the exact wording', state: 'done' },
      { key: 'queue', label: 'Queue it for delivery', state: 'done' },
      { key: 'deliver', label: 'Mail provider confirms delivery', state: 'done' },
    ],
  },
  estimated_completion: '2026-08-15',
  counts: { new: 2, needs_attention: 3 },
  next_action: {
    label: 'Complete record search',
    detail: 'The earliest role-scoped open work item.',
    tab: 'collection',
    work_item_id: 'work-1',
  },
  title: 'Board minutes for the spring term',
  summary: 'Minutes and agendas from every spring board meeting.',
  matter_kind: 'public_records',
  next_due: null,
  stages: [
    { id: 'intake', label: 'Intake', state: 'done', open_count: 0, summary: '', reason: '' },
    { id: 'scope', label: 'Scope', state: 'done', open_count: 0, summary: '', reason: '' },
    {
      id: 'collect', label: 'Collect', state: 'current', open_count: 2,
      summary: '2 open collections', reason: '',
    },
    {
      id: 'review', label: 'Review', state: 'upcoming', open_count: 5,
      summary: '5 records to review', reason: '',
    },
    { id: 'redact', label: 'Redact', state: 'upcoming', open_count: 0, summary: '', reason: '' },
    { id: 'release', label: 'Release', state: 'upcoming', open_count: 0, summary: '', reason: '' },
    { id: 'close', label: 'Close', state: 'upcoming', open_count: 0, summary: '', reason: '' },
  ],
}

const STAGE_NAMES = ['Intake', 'Scope', 'Collect', 'Review', 'Redact', 'Release', 'Close']

function stageLink(name: string) {
  return screen.queryByRole('link', { name: new RegExp(`^${name}\\b`) })
}

const DETAIL = {
  id: 1,
  number: 'PRR-2026-0001',
  status: 'gathering',
  received_at: '2026-07-01T12:00:00Z',
  statutory_response_due: '2026-07-10',
  estimated_completion: '2026-08-15',
  closed_at: null,
  requester_email: 'jane@example.org',
  requester_name: 'Jane Q. Public',
  assigned_officer_username: 'officer',
  tolled_business_days: 0,
  acknowledgment_sent_at: '2026-07-02T12:00:00Z',
  initial_response_delivered: true,
  initial_response_plan: {
    state: 'delivered',
    due_date: '2026-07-10',
    automatic_check: 'Delivered.',
    correspondence_id: null,
    correspondence_status: null,
    delivery_status: null,
    automated: false,
  },
  description: 'Immutable request description.',
  channel: 'email',
  requester: {
    name: 'Jane Q. Public',
    email: 'jane@example.org',
    phone: '',
    mailing_address: '',
    city: '',
    state: '',
    postal_code: '',
    company: '',
  },
  events: [],
  estimate_revisions: [],
  collection_tasks: [],
  footage_preservations: [],
  intake_attachments: [],
  email_intake_auto_created: false,
}

function fakeResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function renderCase(
  route: string,
  roles: StaffRole[] = ['records_officer'],
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
    if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
    if (url.endsWith('/activity/')) return fakeResponse({ results: [] })
    if (url.endsWith('/closure-readiness/')) {
      return fakeResponse({ ready: true, counts: {}, blockers: [] })
    }
    if (url.endsWith('/advisory-intent/')) {
      return fakeResponse({ enabled: true, latest: null })
    }
    if (url.endsWith('/vault-counts/')) {
      return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
    }
    return fakeResponse({ count: 0, next: null, previous: null, results: [] })
  }),
) {
  vi.stubGlobal('fetch', fetchMock)
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const rendered = render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route
              path="/requests/:number/:tab"
              element={<CaseWorkspace roles={roles} />}
            />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
  return { ...rendered, fetchMock }
}

beforeEach(() => {
  document.cookie = 'csrftoken=test-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('CaseWorkspace', () => {
  it('renders the persistent bounded header and the whole rail at a direct URL', async () => {
    renderCase('/requests/PRR-2026-0001/intake')

    // The case opens on what it is, what was asked, and the one next step.
    expect(await screen.findByRole('heading', { name: 'Board minutes for the spring term', level: 1 }))
      .toBeInTheDocument()
    const header = screen.getByRole('region', { name: 'Case summary' })
    expect(header).toHaveTextContent('PRR-2026-0001')
    expect(header).toHaveTextContent('Jane Q. Public')
    expect(header).toHaveTextContent('Case Officer')
    expect(header).toHaveTextContent('Minutes and agendas from every spring board meeting.')
    expect(header).toHaveTextContent('No due date')
    expect(screen.getByRole('link', { name: 'Finish collection' }))
      .toHaveAttribute('href', '/requests/PRR-2026-0001/collect')
    expect(screen.queryByText('2 new')).not.toBeInTheDocument()
    for (const stage of STAGE_NAMES) expect(stageLink(stage)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Messages' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Activity' })).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Intake', level: 2 }))
      .toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Time entries' }))
      .toBeInTheDocument()
  })

  it('loads only the active tab and keeps the case summary cached across navigation', async () => {
    // The case timeline is now the Running log on Overview, so Overview is
    // what must fetch it -- there is no second tab showing the same feed.
    const { fetchMock } = renderCase('/requests/PRR-2026-0001/intake')
    await screen.findByRole('heading', { name: 'Intake', level: 2 })
    // The Running log loads once the case record arrives, so wait for it
    // rather than sampling the calls made so far.
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]))
      expect(urls).toContain('/api/staff/requests/PRR-2026-0001/activity/')
    })
    const requested = fetchMock.mock.calls.map((call) => String(call[0]))
    // Overview legitimately loads the case record and its timeline. What it
    // must not do is pull the other tabs' heavy data.
    expect(requested).toContain('/api/staff/requests/PRR-2026-0001/')
    expect(requested.some((url) => url.includes('/correspondence/'))).toBe(false)
    expect(requested.some((url) => url.includes('/collection-tasks/'))).toBe(false)
    expect(requested.some((url) => url.includes('/record-items/'))).toBe(false)
    expect(requested.some((url) => url.includes('/installments/'))).toBe(false)

    // Focus moves to the new section's heading on a real tab change, so
    // keyboard and screen-reader users land where they navigated to.
    await userEvent.click(screen.getByRole('link', { name: 'Messages' }))
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Messages', level: 2 }))
        .toHaveFocus(),
    )
    expect(fetchMock.mock.calls.filter(
      ([input]) => String(input).endsWith('/workspace-summary/'),
    )).toHaveLength(1)
  })

  it('assigns the case owner through the existing guarded request PATCH', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/staff-users/?role=records_officer') {
        return fakeResponse({
          count: 2,
          next: null,
          previous: null,
          results: [
            { id: 7, username: 'officer', display_name: 'Case Officer' },
            { id: 8, username: 'second', display_name: 'Second Officer' },
          ],
        })
      }
      if (url === '/api/staff/requests/PRR-2026-0001/' && init?.method === 'PATCH') {
        return fakeResponse({
          ...DETAIL,
          assigned_officer_id: 8,
          assigned_officer_username: 'second',
        })
      }
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({ ...DETAIL, assigned_officer_id: 7 })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase('/requests/PRR-2026-0001/intake', ['records_officer'], fetchMock)
    const user = userEvent.setup()

    await user.selectOptions(await screen.findByLabelText('Case owner'), '8')

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0001/',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ assigned_officer_id: 8 }),
      }),
    ))
  })

  it.each([
    ['liaison', ['Intake', 'Collect', 'Close', 'Messages', 'Activity']],
    ['reviewer', ['Intake', 'Review', 'Redact', 'Release', 'Close', 'Activity']],
    ['admin', ['Intake', 'Collect', 'Review', 'Release', 'Close', 'Activity']],
  ] as Array<[StaffRole, string[]]>)('links only the stages %s may open', async (role, visible) => {
    renderCase('/requests/PRR-2026-0001/intake', [role])

    await screen.findByRole('heading', { name: 'Board minutes for the spring term', level: 1 })
    // Every stage is shown, so progress reads the same for everyone; only
    // the ones the role may open are links.
    const rail = screen.getByRole('navigation', { name: 'Case stages' })
    for (const stage of STAGE_NAMES) expect(rail).toHaveTextContent(stage)
    for (const tab of [...STAGE_NAMES, 'Messages', 'Activity']) {
      const link = STAGE_NAMES.includes(tab)
        ? stageLink(tab)
        : screen.queryByRole('link', { name: tab })
      if (visible.includes(tab)) expect(link).toBeInTheDocument()
      else expect(link).not.toBeInTheDocument()
    }
    if (role !== 'records_officer') {
      expect(screen.queryByText('Jane Q. Public')).not.toBeInTheDocument()
    }
  })

  it('shows a useful denial without mounting an unauthorized direct tab', async () => {
    const { fetchMock } = renderCase(
      '/requests/PRR-2026-0001/messages',
      ['reviewer'],
    )

    expect(await screen.findByRole('heading', { name: 'Not authorized' }))
      .toBeInTheDocument()
    expect(fetchMock.mock.calls.some(
      ([input]) => String(input).includes('/correspondence/'),
    )).toBe(false)
  })

  it.each([
    [
      '/requests/PRR-2026-0001/collection?task=73',
      '/api/staff/collection-tasks/73/?request=PRR-2026-0001',
    ],
    [
      '/requests/PRR-2026-0001/records?item=00000000-0000-4000-8000-000000000073',
      '/api/staff/record-items/00000000-0000-4000-8000-000000000073/?request=PRR-2026-0001',
    ],
  ])('scopes selected nested objects to the case URL', async (route, expected) => {
    const { fetchMock } = renderCase(route)

    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input) === expected,
    )).toBe(true))
  })

  it.each([
    [
      '/requests/PRR-2026-0001/intake',
      '/api/staff/staff-users/?role=records_officer',
      '/api/staff/staff-users/?role=records_officer&page=2',
    ],
    [
      '/requests/PRR-2026-0001/collect',
      '/api/staff/collection-tasks/?request=PRR-2026-0001',
      '/api/staff/collection-tasks/?request=PRR-2026-0001&page=2',
    ],
    [
      '/requests/PRR-2026-0001/release',
      '/api/staff/installments/?request=PRR-2026-0001',
      '/api/staff/installments/?request=PRR-2026-0001&page=2',
    ],
  ])('loads every page needed by the active stage', async (route, first, next) => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/closure-readiness/')) {
        return fakeResponse({ ready: true, counts: {}, blockers: [] })
      }
      if (url === first) {
        return fakeResponse({ count: 51, next, previous: null, results: [] })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })

    renderCase(route, ['records_officer'], fetchMock)

    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input) === next,
    )).toBe(true))
  })

  it('pages the records tab on demand instead of pulling every page at once', async () => {
    // Records deliberately differ from the other tabs. A collection can run to
    // thousands of items, and walking every `next` link means that many serial
    // round trips before a single row appears -- and then every row in the DOM
    // at once. The reviewer gets one page plus a stated total instead.
    const first = '/api/staff/record-items/?request=PRR-2026-0001'
    const next = '/api/staff/record-items/?request=PRR-2026-0001&page=2'
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/closure-readiness/')) {
        return fakeResponse({ ready: true, counts: {}, blockers: [] })
      }
      if (url === first) {
        return fakeResponse({ count: 51, next, previous: null, results: [] })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })

    renderCase('/requests/PRR-2026-0001/review?view=records', ['records_officer'], fetchMock)

    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input) === first,
    )).toBe(true))
    expect(fetchMock.mock.calls.some(([input]) => String(input) === next)).toBe(false)
  })

  it('keeps the admin record inventory read-only without restricted detail calls', async () => {
    const item = {
      id: '00000000-0000-4000-8000-000000000099',
      request_number: 'PRR-2026-0001',
      source_system: 'Google Drive',
      custodian: 'Records Office',
      original_filename: 'inventory-only.pdf',
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
      if (url.endsWith('/workspace-summary/')) {
        return fakeResponse({ ...SUMMARY, requester: null })
      }
      if (url === '/api/staff/record-items/?request=PRR-2026-0001') {
        return fakeResponse({ count: 1, next: null, previous: null, results: [item] })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase('/requests/PRR-2026-0001/review', ['admin'], fetchMock)

    await userEvent.click(await screen.findByText('inventory-only.pdf'))
    expect(await screen.findByRole('dialog', { name: /inventory-only\.pdf/i }))
      .toBeInTheDocument()

    const requested = fetchMock.mock.calls.map(([input]) => String(input))
    expect(requested.some((url) => url.includes('/preview-metadata/'))).toBe(false)
    expect(requested.some((url) => url.includes('/redactions/'))).toBe(false)
  })

  it('shows liaisons a preservation handoff without an unauthorized global link', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) {
        return fakeResponse({ ...SUMMARY, requester: null })
      }
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({
          ...DETAIL,
          requester: null,
          footage_preservations: [{ id: 9, state: 'held' }],
        })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase('/requests/PRR-2026-0001/collect', ['liaison'], fetchMock)

    expect(await screen.findByText(/Records Officer handoff required/i))
      .toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /View in Preservations/i }))
      .not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(
      ([input]) => String(input).includes('/api/staff/preservations/'),
    )).toBe(false)
  })

  it('warns that footage may be overwritten when a named site has no Aware cameras', async () => {
    // Nothing was bookmarked, but a standalone recorder at the site may still
    // be overwriting footage, so the adequate-search review is urgent.
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) {
        return fakeResponse({ ...SUMMARY, requester: null })
      }
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({
          ...DETAIL,
          requester: null,
          footage_preservations: [
            { id: 9, state: 'no_cameras', confirmation_text: null },
          ],
        })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase('/requests/PRR-2026-0001/collection', ['liaison'], fetchMock)

    expect(await screen.findByText(
      /Footage may be overwritten — No cameras in Aware at the requested site/,
    )).toBeInTheDocument()
  })

  it.each([
    {
      name: 'an expired window needs an adequate-search review',
      preservation: { id: 9, state: 'expired', confirmation_text: null },
      label: 'Window outside retention — adequate-search review required',
      tone: 'nx-badge--danger',
    },
    {
      name: 'a release whose bookmark deletes failed is not plainly "Released"',
      preservation: {
        id: 9, state: 'released', confirmation_text: null, has_release_failures: true,
      },
      label: 'Not yet confirmed deleted — bookmark may still exist; retry the release',
      tone: 'nx-badge--danger',
    },
    {
      name: 'a clean release reads Released',
      preservation: {
        id: 9, state: 'released', confirmation_text: null, has_release_failures: false,
      },
      label: 'Released',
      tone: 'nx-badge--plain',
    },
  ])('labels the case footage card like Preservations: $name', async ({ preservation, label, tone }) => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) {
        return fakeResponse({ ...SUMMARY, requester: null })
      }
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({ ...DETAIL, requester: null, footage_preservations: [preservation] })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase('/requests/PRR-2026-0001/collection', ['liaison'], fetchMock)

    const badge = await screen.findByText(label)
    expect(badge).toHaveClass('nx-badge', tone)
  })

  it('shows what Aware confirmed held on the case summary', async () => {
    const confirmation =
      '3 cameras across the district held from 2026-09-22 00:00 to 2026-09-23 00:00 '
      + 'Pacific time (Aware bookmarks, no expiry); 0 cameras have footage gaps; 0 failed.'
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) {
        return fakeResponse({ ...SUMMARY, requester: null })
      }
      if (url === '/api/staff/requests/PRR-2026-0001/') {
        return fakeResponse({
          ...DETAIL,
          requester: null,
          footage_preservations: [
            { id: 9, state: 'held', confirmation_text: confirmation },
          ],
        })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase('/requests/PRR-2026-0001/collection', ['liaison'], fetchMock)

    expect(await screen.findByText(confirmation)).toBeInTheDocument()
  })

  it('starts every case with its own Vault end date after a client-side hop', async () => {
    // The route element is reused when only :number changes, so any form
    // state the Collection tab keeps for one case would otherwise bleed into
    // the next one an officer opens without a page load.
    const SECOND = {
      ...DETAIL, id: 2, number: 'PRR-2026-0002', received_at: '2026-08-03T12:00:00Z',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url === '/api/staff/requests/PRR-2026-0002/') return fakeResponse(SECOND)
      if (url.endsWith('/advisory-intent/')) {
        return fakeResponse({ enabled: true, latest: null })
      }
      if (url.endsWith('/vault-counts/')) {
        return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={qc}>
        <ToastProvider>
          <MemoryRouter initialEntries={['/requests/PRR-2026-0001/scope']}>
            <CaseHops />
            <Routes>
              <Route
                path="/requests/:number/:tab"
                element={<CaseWorkspace roles={['records_officer']} />}
              />
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </QueryClientProvider>,
    )
    const user = userEvent.setup()

    expect(await screen.findByLabelText('End date')).toHaveValue('2026-07-01')

    // A first visit shows the loading spinner, which happens to remount the
    // tab. The return trip is served from the cached summary and does not.
    await user.click(screen.getByRole('button', { name: 'Open PRR-2026-0002' }))
    await waitFor(() =>
      expect(screen.getByLabelText('End date')).toHaveValue('2026-08-03'),
    )

    // One case's default must not travel back with the officer.
    await user.click(screen.getByRole('button', { name: 'Open PRR-2026-0001' }))
    await waitFor(() =>
      expect(screen.getByLabelText('End date')).toHaveValue('2026-07-01'),
    )

    // And a field cleared on one case must not suppress the next case's default.
    await user.clear(screen.getByLabelText('End date'))
    await user.click(screen.getByRole('button', { name: 'Open PRR-2026-0002' }))
    await waitFor(() =>
      expect(screen.getByLabelText('End date')).toHaveValue('2026-08-03'),
    )
  })
  it('does not carry one case\'s release selection into another case', async () => {
    // The Releases tab keeps the officer's record selection in component
    // state. A return trip to a case is served from the cached summary and
    // never passes through the loading spinner, so without a per-case key the
    // tab would keep the previous case's selection -- and post those record
    // ids against this case's number.
    const readyRecord = {
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      original_filename: 'board-minutes.pdf',
      custodian: 'superintendent@psd401.net',
      review_state: 'responsive',
      content_length: 2048,
      redaction_count: 0,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url === '/api/staff/requests/PRR-2026-0002/') {
        return fakeResponse({ ...DETAIL, id: 2, number: 'PRR-2026-0002' })
      }
      if (url.endsWith('/closure-readiness/')) {
        return fakeResponse({ ready: true, counts: {}, blockers: [] })
      }
      if (url.startsWith('/api/staff/record-items/')) {
        return fakeResponse({
          count: 1, next: null, previous: null, results: [readyRecord],
        })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={qc}>
        <ToastProvider>
          <MemoryRouter initialEntries={['/requests/PRR-2026-0001/release']}>
            <ReleaseHops />
            <Routes>
              <Route
                path="/requests/:number/:tab"
                element={<CaseWorkspace roles={['records_officer']} />}
              />
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </QueryClientProvider>,
    )
    const user = userEvent.setup()

    await user.click(await screen.findByRole('checkbox', { name: /select all/i }))
    expect(screen.getByRole('button', { name: 'Create installment (1)' }))
      .toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Releases for PRR-2026-0002' }))
    await user.click(await screen.findByRole('checkbox', { name: /select all/i }))
    expect(screen.getByRole('button', { name: 'Create installment (1)' }))
      .toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Releases for PRR-2026-0001' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create installment (0)' }))
        .toBeInTheDocument(),
    )
  })
})

function ReleaseHops() {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => navigate('/requests/PRR-2026-0001/release')}>
        Releases for PRR-2026-0001
      </button>
      <button type="button" onClick={() => navigate('/requests/PRR-2026-0002/release')}>
        Releases for PRR-2026-0002
      </button>
    </>
  )
}

function CaseHops() {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => navigate('/requests/PRR-2026-0001/scope')}>
        Open PRR-2026-0001
      </button>
      <button type="button" onClick={() => navigate('/requests/PRR-2026-0002/scope')}>
        Open PRR-2026-0002
      </button>
    </>
  )
}

function LocationProbe() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output>{location.pathname}</output>
      <button type="button" onClick={() => navigate(-1)}>Back</button>
    </>
  )
}

describe('LegacyRequestRedirect', () => {
  it('replaces history with the reloadable intake URL', async () => {
    render(
      <MemoryRouter
        initialEntries={['/cases', '/requests/PRR-2026-0001']}
        initialIndex={1}
      >
        <Routes>
          <Route path="/requests/:number" element={<LegacyRequestRedirect />} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(await screen.findByText('/requests/PRR-2026-0001/intake'))
      .toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByText('/cases')).toBeInTheDocument()
  })
})

describe('CaseWorkspace record uploads', () => {
  const RECORDS = '/requests/PRR-2026-0001/collect'

  it('offers the upload button to a Records Officer on the Collect stage', async () => {
    renderCase(RECORDS, ['records_officer'])

    expect(await screen.findByRole('button', { name: 'Upload records' }))
      .toBeEnabled()
  })

  it.each([['liaison'], ['admin']] as Array<[StaffRole]>)(
    'hides the upload button from %s',
    async (role) => {
      renderCase(RECORDS, [role])

      await screen.findByRole('heading', { name: 'Collect', level: 2 })
      expect(screen.queryByRole('button', { name: 'Upload records' }))
        .not.toBeInTheDocument()
    },
  )

  it('disables the upload on a closed case', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) {
        return fakeResponse({ ...SUMMARY, legal_status: 'fulfilled' })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase(RECORDS, ['records_officer'], fetchMock)

    expect(await screen.findByRole('button', { name: 'Upload records' }))
      .toBeDisabled()
    expect(screen.getByText('This case is closed. Reopen it to add records.'))
      .toBeInTheDocument()
  })

  it('posts the chosen files as multipart and shows each file outcome', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/record-uploads/') && init?.method === 'POST') {
        return fakeResponse({
          added_count: 1,
          results: [
            {
              filename: 'packet.pdf',
              outcome: 'added',
              detail: '',
              record_item_id: '00000000-0000-4000-8000-000000000001',
              sha256: 'a'.repeat(64),
              size: 9,
            },
            {
              filename: 'evil.pdf',
              outcome: 'infected',
              detail: 'Malware was detected: Eicar-Test-Signature',
              record_item_id: null,
              sha256: 'b'.repeat(64),
              size: 8,
            },
          ],
        })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase(RECORDS, ['records_officer'], fetchMock)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Upload records' }))
    const packet = new File(['%PDF-1.4'], 'packet.pdf', { type: 'application/pdf' })
    const evil = new File(['%PDF-bad'], 'evil.pdf', { type: 'application/pdf' })
    await user.upload(screen.getByLabelText('Files'), [packet, evil])
    await user.click(screen.getByRole('button', { name: 'Upload' }))

    const results = await screen.findByRole('list', { name: 'Upload results' })
    expect(results).toHaveTextContent('packet.pdf')
    expect(results).toHaveTextContent('Added')
    expect(results).toHaveTextContent('Rejected: malware detected')
    expect(results).toHaveTextContent('Eicar-Test-Signature')

    const call = fetchMock.mock.calls.find(
      ([input, init]) => String(input) === '/api/staff/requests/PRR-2026-0001/record-uploads/'
        && init?.method === 'POST',
    )
    expect(call).toBeDefined()
    const body = call![1]!.body as FormData
    expect(body).toBeInstanceOf(FormData)
    expect((body.getAll('files') as File[]).map((file) => file.name))
      .toEqual(['packet.pdf', 'evil.pdf'])
    expect((call![1]!.headers as Record<string, string>)['X-CSRFToken'])
      .toBe('test-token')
  })

  it('reports files refused because the case closed or authority lapsed mid-batch', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/record-uploads/') && init?.method === 'POST') {
        return fakeResponse({
          added_count: 1,
          results: [
            {
              filename: 'one.pdf', outcome: 'added', detail: '',
              record_item_id: '00000000-0000-4000-8000-000000000001',
              sha256: 'a'.repeat(64), size: 3,
            },
            {
              filename: 'two.pdf', outcome: 'case_closed',
              detail: 'This request is closed (fulfilled); reopen it before adding records.',
              record_item_id: null, sha256: 'b'.repeat(64), size: 3,
            },
            {
              filename: 'three.pdf', outcome: 'not_authorized',
              detail: 'You no longer hold Records Officer authority for this case.',
              record_item_id: null, sha256: 'c'.repeat(64), size: 3,
            },
          ],
        })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase(RECORDS, ['records_officer'], fetchMock)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Upload records' }))
    await user.upload(screen.getByLabelText('Files'), [
      new File(['one'], 'one.pdf', { type: 'application/pdf' }),
      new File(['two'], 'two.pdf', { type: 'application/pdf' }),
      new File(['three'], 'three.pdf', { type: 'application/pdf' }),
    ])
    await user.click(screen.getByRole('button', { name: 'Upload' }))

    const results = await screen.findByRole('list', { name: 'Upload results' })
    expect(results).toHaveTextContent('Added')
    expect(results).toHaveTextContent('Not added: case was closed')
    expect(results).toHaveTextContent('Not added: no longer authorized')
    expect(results).toHaveTextContent('reopen it before adding records')
  })

  it('shows the server refusal when the whole upload is over a limit', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url.endsWith('/record-uploads/') && init?.method === 'POST') {
        return fakeResponse({
          code: 'multipart_upload_limit_exceeded',
          files: ['A multipart file exceeds the per-file size limit.'],
        }, 413)
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
    renderCase(RECORDS, ['records_officer'], fetchMock)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Upload records' }))
    await user.upload(
      screen.getByLabelText('Files'),
      new File(['x'], 'huge.pdf', { type: 'application/pdf' }),
    )
    await user.click(screen.getByRole('button', { name: 'Upload' }))

    expect(await screen.findByRole('alert'))
      .toHaveTextContent('A multipart file exceeds the per-file size limit.')
  })
})

describe('CaseWorkspace stage rail (#45)', () => {
  const VAULT_TASK = {
    id: 41,
    kind: 'vault_mail',
    state: 'complete',
    source_label: 'Google Workspace (Gmail)',
    records_created: 120,
    records_unreviewed: 37,
    search_terms: 'levy',
    created_at: '2026-07-03T12:00:00Z',
    completed_at: '2026-07-04T12:00:00Z',
  }

  function withCollections(tasks: unknown[], extra?: (url: string, init?: RequestInit) => Response | null) {
    return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const handled = extra?.(url, init)
      if (handled) return handled
      if (url.endsWith('/workspace-summary/')) return fakeResponse(SUMMARY)
      if (url === '/api/staff/requests/PRR-2026-0001/') return fakeResponse(DETAIL)
      if (url === '/api/staff/collection-tasks/?request=PRR-2026-0001') {
        return fakeResponse({ count: tasks.length, next: null, previous: null, results: tasks })
      }
      if (url.endsWith('/closure-readiness/')) {
        return fakeResponse({ ready: true, counts: {}, blockers: [] })
      }
      if (url.endsWith('/advisory-intent/')) return fakeResponse({ enabled: true, latest: null })
      if (url.endsWith('/vault-counts/')) {
        return fakeResponse({ enabled: true, corpus: 'MAIL', latest: null })
      }
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    })
  }

  it('shows each stage state, marks the open stage, and gives a blocked reason', async () => {
    const blocked = {
      ...SUMMARY,
      stages: SUMMARY.stages.map((stage) => (stage.id === 'collect'
        ? { ...stage, state: 'blocked', reason: "Waiting on the requester's deposit" }
        : stage)),
    }
    renderCase('/requests/PRR-2026-0001/review', ['records_officer'], vi.fn(async (input) => {
      const url = String(input)
      if (url.endsWith('/workspace-summary/')) return fakeResponse(blocked)
      return fakeResponse({ count: 0, next: null, previous: null, results: [] })
    }))

    expect(await screen.findByRole('link', { name: 'Intake: Done' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: "Collect: Blocked, Waiting on the requester's deposit" }))
      .toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Review: Upcoming, 5 records to review' }))
      .toHaveAttribute('aria-current', 'page')
  })

  it('heads the stage with what it asks, and jumps to the work when already there', async () => {
    renderCase('/requests/PRR-2026-0001/collect', ['records_officer'], withCollections([VAULT_TASK]))

    const card = await screen.findByRole('region', { name: 'Collect decision' })
    expect(card).toHaveTextContent('Finish the open collections')
    expect(card).toHaveTextContent('2 open collections')
    expect(card).toHaveTextContent('Supersede takes unreviewed records out of review; nothing is deleted.')
    expect(card).not.toHaveTextContent('Your call')
    expect(screen.queryByRole('link', { name: 'See the evidence' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Finish collection' })).toHaveAttribute('href', '#stage-evidence')
  })

  it('offers no next step on a closed case', async () => {
    const closed = {
      ...SUMMARY,
      legal_status: 'fulfilled',
      stages: SUMMARY.stages.map((stage) => ({ ...stage, state: 'done' })),
    }
    renderCase('/requests/PRR-2026-0001/intake', ['records_officer'], withCollections([], (url) => (
      url.endsWith('/workspace-summary/') ? fakeResponse(closed) : null
    )))

    await screen.findByRole('heading', { name: 'Board minutes for the spring term', level: 1 })
    expect(screen.queryByRole('link', { name: 'Finish collection' })).not.toBeInTheDocument()
    expect(screen.queryByText('No due date')).not.toBeInTheDocument()
  })

  it('says a finished stage is done', async () => {
    renderCase('/requests/PRR-2026-0001/scope', ['records_officer'], withCollections([VAULT_TASK]))

    const card = await screen.findByRole('region', { name: 'Scope decision' })
    expect(card).toHaveTextContent('Scope is done')
    expect(card).toHaveTextContent('Nothing is open at this stage.')
    expect(card).not.toHaveTextContent('Your call')
  })

  it('leads Collect with one card per collection and its actions on the card', async () => {
    const fetchMock = withCollections([VAULT_TASK], (url, init) => (
      url === '/api/staff/collection-tasks/41/supersede/' && init?.method === 'POST'
        ? fakeResponse({ collection_task_id: 41, record_count: 37 })
        : null
    ))
    renderCase('/requests/PRR-2026-0001/collect', ['records_officer'], fetchMock)
    const user = userEvent.setup()

    const cards = await screen.findByRole('list', { name: 'Collections' })
    expect(cards).toHaveTextContent('Google Workspace (Gmail)')
    expect(cards).toHaveTextContent('Complete')
    expect(cards).toHaveTextContent('120')
    expect(cards).toHaveTextContent('37')
    expect(screen.getByRole('link', { name: 'View records' })).toHaveAttribute(
      'href', '/requests/PRR-2026-0001/review?view=records&collection_task=41',
    )
    expect(screen.getByRole('link', { name: 'Re-scope' }))
      .toHaveAttribute('href', '/requests/PRR-2026-0001/scope')

    // Supersede opens on the card, not behind a row click or a dialog.
    await user.click(screen.getByRole('button', { name: 'Supersede' }))
    const submit = screen.getByRole('button', { name: 'Supersede unreviewed records' })
    expect(submit).toBeDisabled()
    expect(screen.getByText('This takes 37 unreviewed records out of review.')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Reason'), 'The export matched every staff mailbox.')
    await user.click(submit)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/collection-tasks/41/supersede/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ reason: 'The export matched every staff mailbox.' }),
      }),
    ))
  })

  it('keeps Supersede and Re-scope from a liaison', async () => {
    renderCase('/requests/PRR-2026-0001/collect', ['liaison'], withCollections([VAULT_TASK]))

    await screen.findByRole('list', { name: 'Collections' })
    expect(screen.getByRole('link', { name: 'View records' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Supersede' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Re-scope' })).not.toBeInTheDocument()
  })

  it('folds the scoping inputs away once a collection exists', async () => {
    const { unmount } = renderCase(
      '/requests/PRR-2026-0001/scope', ['records_officer'], withCollections([]),
    )
    const open = await screen.findByText('Scope: request intent and Vault count')
    expect(open.closest('details')).toHaveAttribute('open')
    unmount()

    renderCase('/requests/PRR-2026-0001/scope', ['records_officer'], withCollections([VAULT_TASK]))
    const folded = await screen.findByText('Scope: request intent and Vault count')
    expect(folded.closest('details')).not.toHaveAttribute('open')
  })

  it.each([
    ['/requests/PRR-2026-0001/overview', '/requests/PRR-2026-0001/intake'],
    ['/requests/PRR-2026-0001/collection?task=7', '/requests/PRR-2026-0001/collect?task=7'],
    ['/requests/PRR-2026-0001/records?record_state=unreviewed', '/requests/PRR-2026-0001/review?record_state=unreviewed&view=records'],
    ['/requests/PRR-2026-0001/releases', '/requests/PRR-2026-0001/release'],
  ])('redirects the old tab URL %s', async (from, to) => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({ count: 0, results: [] })))
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={[from]}>
          <Routes>
            <Route
              path="/requests/:number/:tab"
              element={<><CaseWorkspace roles={['records_officer']} /><SearchProbe /></>}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByText(to)).toBeInTheDocument()
  })

  it('offers the one-by-one flight and the record list on Review', async () => {
    renderCase('/requests/PRR-2026-0001/review', ['reviewer'])
    const user = userEvent.setup()

    const flight = await screen.findByRole('button', { name: 'Review one by one' })
    expect(flight).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'Record list' }))
    expect(screen.getByRole('button', { name: 'Record list' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('shows an admin the record list without the flight toggle', async () => {
    const { fetchMock } = renderCase('/requests/PRR-2026-0001/review', ['admin'])

    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input).startsWith('/api/staff/record-items/?request=PRR-2026-0001'),
    )).toBe(true))
    expect(screen.queryByRole('button', { name: 'Review one by one' })).not.toBeInTheDocument()
  })

  it('lists one collection when a card sends the officer to its records', async () => {
    const { fetchMock } = renderCase(
      '/requests/PRR-2026-0001/review?view=records&collection_task=41', ['records_officer'],
    )
    const user = userEvent.setup()

    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input) === '/api/staff/record-items/?request=PRR-2026-0001&collection_task=41',
    )).toBe(true))
    // Inside a case the proposals belong to Redact, not the record list.
    expect(fetchMock.mock.calls.some(
      ([input]) => String(input).includes('/redaction-batches/groups/'),
    )).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Show every collection' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input) === '/api/staff/record-items/?request=PRR-2026-0001',
    )).toBe(true))
  })

  it('puts the case redaction proposals on the Redact stage', async () => {
    const { fetchMock } = renderCase('/requests/PRR-2026-0001/redact', ['reviewer'])

    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input) === '/api/staff/redaction-batches/groups/?request=PRR-2026-0001',
    )).toBe(true))
  })

  it('carries the closure readiness report and the closing action on Close', async () => {
    const { fetchMock } = renderCase('/requests/PRR-2026-0001/close', ['records_officer'])

    expect(await screen.findByText('No listed blockers')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Close case…' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(
      ([input]) => String(input).endsWith('/closure-readiness/'),
    )).toBe(true)
  })

  it('gives a liaison the closing action without the officer readiness report', async () => {
    const { fetchMock } = renderCase('/requests/PRR-2026-0001/close', ['liaison'])

    expect(await screen.findByRole('button', { name: 'Close case…' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(
      ([input]) => String(input).endsWith('/closure-readiness/'),
    )).toBe(false)
  })
})

function SearchProbe() {
  const location = useLocation()
  return <output>{`${location.pathname}${location.search}`}</output>
}
