// ABOUTME: Interaction coverage for the request-scoped correspondence workspace.
// ABOUTME: Proves timeline, drafting, human gates, delivery, and clarification flows.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import { ToastProvider } from '../ui'
import {
  page as pageOf,
  renderPage as renderHarness,
} from '../test/pageHarness'
import type { StatutoryResponse } from '../statutory'
import { CorrespondencePanel } from './CorrespondencePanel'
import { correspondenceDetailQueryKey } from '../correspondence'

function fakeResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

const PAGE = (results: unknown[]) => ({
  count: results.length,
  next: null,
  previous: null,
  results,
})

const BASE_SUMMARY = {
  request_number: 'PRR-2026-0001',
  automated: false,
  approved_at: null,
  queued_at: null,
  sent_at: null,
  received_at: null,
  matched_at: null,
  failed_at: null,
  cancelled_at: null,
  failure_category: '',
  version: 1,
  recipient_count: 1,
  attachment_count: 0,
  delivery_status: null,
  resulting_status_event_id: null,
}

const MESSAGE_PAGE = (results: Array<Record<string, any>>) => PAGE(
  results.map((item) => ({
    ...BASE_SUMMARY,
    id: item.id,
    request_number: item.request_number,
    direction: item.direction,
    kind: item.kind,
    status: item.status,
    subject: item.direction === 'inbound'
      ? item.received_subject
      : item.approved_subject || item.draft_subject,
    snippet: item.direction === 'inbound'
      ? item.received_body
      : item.approved_body || item.draft_body,
    automated: item.automated,
    created_at: item.created_at,
    approved_at: item.approved_at,
    queued_at: item.queued_at,
    sent_at: item.sent_at,
    received_at: item.received_at,
    matched_at: item.matched_at,
    failed_at: item.failed_at,
    cancelled_at: item.cancelled_at,
    failure_category: item.failure_category,
    version: item.version,
    recipient_count: item.recipient_count,
    attachment_count: item.attachment_count,
    delivery_status: item.delivery_status,
    matching_status: item.matching_status ?? null,
    resulting_status_event_id: item.resulting_status_event_id,
    inbound_receipt_id: item.inbound_receipt_id ?? null,
    work_item: item.work_item ?? null,
  })),
)

const OUTBOUND = {
  ...BASE_SUMMARY,
  id: 'outbound-1',
  direction: 'outbound',
  kind: 'general',
  status: 'failed',
  created_at: '2026-07-02T12:00:00Z',
  failed_at: '2026-07-02T12:05:00Z',
  failure_category: 'provider_unavailable',
  attachment_count: 1,
  delivery_status: 'retryable',
}

const INBOUND = {
  ...BASE_SUMMARY,
  id: 'inbound-1',
  direction: 'inbound',
  kind: 'clarification_response',
  status: 'received',
  created_at: '2026-07-03T12:00:00Z',
  received_at: '2026-07-03T12:00:00Z',
  matched_at: '2026-07-03T12:01:00Z',
}

const OUTBOUND_DETAIL = {
  ...OUTBOUND,
  resulting_status_event_id: 42,
  resulting_status: 'gathering',
  draft_subject: 'Case update',
  draft_body: 'The search is continuing.\n',
  draft_metadata: {},
  approved_subject: 'Case update',
  approved_body: 'The search is continuing.\n',
  approved_metadata: {},
  received_subject: '',
  received_body: '',
  received_sender: '',
  received_recipients: [],
  template_identifier: '',
  template_version: '',
  created_by: 'liaison',
  approved_by: 'officer',
  queued_by: 'officer',
  matched_by: null,
  rfc_message_id: '<outbound-1@prr.psd401.net>',
  provider: 'gmail',
  provider_account: 'records@psd401.net',
  provider_message_id: '',
  provider_thread_id: '',
  failure_description: 'Gmail was temporarily unavailable.',
  replaces_id: null,
  recipients: [
    {
      id: 1,
      recipient_type: 'to',
      email: 'jane@example.org',
      display_name: 'Jane Requester',
      requester_relationship: true,
      created_at: '2026-07-02T12:00:00Z',
    },
  ],
  attachments: [
    {
      id: 'attachment-1',
      original_filename: 'context.txt',
      content_type: 'text/plain',
      size: 12,
      sha256: 'a'.repeat(64),
      scan_status: 'clean',
      quarantined: false,
      quarantine_reason: '',
      is_downloadable: true,
      created_at: '2026-07-02T12:00:00Z',
      scanned_at: '2026-07-02T12:00:30Z',
    },
  ],
  delivery: {
    id: 'delivery-1',
    status: 'retryable',
    provider: 'gmail',
    provider_account: 'records@psd401.net',
    claim_expires_at: null,
    attempt_count: 1,
    successful_attempt_number: null,
    last_failure_category: 'provider_unavailable',
    last_failure_description: 'Gmail was temporarily unavailable.',
    provider_message_id: '',
    provider_thread_id: '',
    queued_at: '2026-07-02T12:01:00Z',
    completed_at: '2026-07-02T12:05:00Z',
    version: 2,
  },
}

const INBOUND_DETAIL = {
  ...INBOUND,
  draft_subject: '',
  draft_body: '',
  draft_metadata: {},
  approved_subject: '',
  approved_body: '',
  approved_metadata: {},
  received_subject: 'Re: Case update',
  received_body: 'School years 2024 through 2026.\n',
  template_identifier: '',
  template_version: '',
  created_by: null,
  approved_by: null,
  queued_by: null,
  matched_by: 'officer',
  rfc_message_id: '<requester-reply@example.org>',
  provider: 'gmail',
  provider_account: 'records@psd401.net',
  provider_message_id: 'gmail-inbound-1',
  provider_thread_id: 'thread-1',
  received_sender: 'Community Advocate <advocate@community.example.org>',
  received_recipients: [
    'Public Records <publicrecords@psd401.net>',
    'Records Officer <officer@psd401.net>',
  ],
  failure_description: '',
  replaces_id: null,
  recipients: [],
  attachments: [],
  delivery: null,
}

const RESPONDED_CYCLE = {
  id: 'cycle-1',
  request_number: 'PRR-2026-0001',
  outbound_correspondence_id: 'outbound-1',
  inbound_response_correspondence_id: 'inbound-1',
  opened_at: '2026-07-02T12:02:00Z',
  responded_at: '2026-07-03T12:00:00Z',
  resolved_at: null,
  cancelled_at: null,
}

function renderPanel(
  roles: StaffRole[] = ['records_officer'],
  options: {
    entry?: string
    statutoryResponse?: StatutoryResponse
  } = {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return {
    ...render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={[options.entry ?? '/']}>
          <CorrespondencePanel
            requestNumber="PRR-2026-0001"
            roles={roles}
            requesterEmail="jane@example.org"
            requesterName="District Info Mailbox"
            statutoryResponse={options.statutoryResponse}
          />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
    ),
    queryClient: qc,
  }
}

const STATUTORY_STEPS = [
  { key: 'prepare', label: 'Prepare the acknowledgment draft' },
  { key: 'approve', label: 'Records Officer approves the exact wording' },
  { key: 'queue', label: 'Queue it for delivery' },
  { key: 'deliver', label: 'Mail provider confirms delivery' },
]

/** The plan as the staff API reports it, with `current` on one named step. */
function statutoryResponse(
  state: string,
  currentKey: string,
  correspondenceId: string | null = null,
): StatutoryResponse {
  const currentIndex = STATUTORY_STEPS.findIndex((step) => step.key === currentKey)
  return {
    state,
    due_date: '2026-09-04',
    next_action: 'Prepare and send the acknowledgment.',
    correspondence_id: correspondenceId,
    steps: STATUTORY_STEPS.map((step, index) => ({
      ...step,
      state: index < currentIndex
        ? 'done' as const
        : index === currentIndex
          ? 'current' as const
          : 'pending' as const,
    })),
  }
}

beforeEach(() => {
  document.cookie = 'csrftoken=test-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('CorrespondencePanel', () => {
  function stubEmptyWorkspace() {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/?request=PRR-2026-0001')) {
        return fakeResponse(MESSAGE_PAGE([]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))
  }

  it('opens the composer preset when a link asks for a specific message kind', async () => {
    stubEmptyWorkspace()

    renderPanel(['records_officer'], { entry: '/?compose=acknowledgment' })

    expect(await screen.findByRole('dialog', { name: 'Compose correspondence' }))
      .toBeVisible()
    expect(screen.getByLabelText('Message type')).toHaveValue('acknowledgment')
  })

  it('ignores a compose link for a kind the role may not draft', async () => {
    stubEmptyWorkspace()

    renderPanel(['liaison'], { entry: '/?compose=denial' })

    await screen.findByRole('button', { name: 'Compose message' })
    expect(screen.queryByRole('dialog', { name: 'Compose correspondence' }))
      .not.toBeInTheDocument()
  })

  it('shows the statutory steps on Messages and opens the acknowledgment', async () => {
    stubEmptyWorkspace()
    const user = userEvent.setup()

    renderPanel(['records_officer'], {
      statutoryResponse: statutoryResponse('manual_required', 'prepare'),
    })

    const steps = await screen.findByRole('list', { name: 'Statutory response steps' })
    expect(steps).toHaveTextContent('Prepare the acknowledgment draft')
    expect(steps).toHaveTextContent('Mail provider confirms delivery')
    expect(screen.getByText(/Five-business-day response due 2026-09-04/))
      .toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Prepare acknowledgment' }))

    expect(await screen.findByRole('dialog', { name: 'Compose correspondence' }))
      .toBeVisible()
    expect(screen.getByLabelText('Message type')).toHaveValue('acknowledgment')
  })

  it('sends an approved-but-unqueued acknowledgment to its own message, not a new draft', async () => {
    stubEmptyWorkspace()
    const user = userEvent.setup()

    renderPanel(['records_officer'], {
      statutoryResponse: statutoryResponse(
        'approved_not_queued',
        'queue',
        'ack-1',
      ),
    })

    await user.click(
      await screen.findByRole('button', {
        name: 'Open the approved acknowledgment to send it',
      }),
    )

    // Queueing stays the existing confirmed action on the message itself; the
    // banner only navigates to it. Approval alone is not a response.
    expect(screen.queryByRole('dialog', { name: 'Compose correspondence' }))
      .not.toBeInTheDocument()
  })

  it('offers no statutory call to action once the response is delivered', async () => {
    stubEmptyWorkspace()

    renderPanel(['records_officer'], {
      statutoryResponse: {
        ...statutoryResponse('delivered', 'prepare'),
        steps: STATUTORY_STEPS.map((step) => ({ ...step, state: 'done' as const })),
        next_action: '',
      },
    })

    expect(await screen.findByText('Initial response delivered')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Prepare acknowledgment' }))
      .not.toBeInTheDocument()
  })

  it('prevents Escape from hiding a delivery confirmation while its action is pending', async () => {
    let resolveRetry!: (response: Response) => void
    const pendingRetry = new Promise<Response>((resolve) => {
      resolveRetry = resolve
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([OUTBOUND_DETAIL]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (url === '/api/staff/correspondence/outbound-1/?request=PRR-2026-0001' && method === 'GET') {
        return fakeResponse(OUTBOUND_DETAIL)
      }
      if (url.endsWith('/retry/') && method === 'POST') return pendingRetry
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    const { queryClient } = renderPanel()
    queryClient.setQueryData(
      ['case-closure-readiness', 'PRR-2026-0001'],
      { ready: false },
    )
    await user.click(await screen.findByRole('button', { name: 'Open Case update' }))
    await user.click(await screen.findByRole('button', { name: 'Retry Case update' }))
    const dialog = screen.getByRole('dialog', { name: 'Retry correspondence delivery' })
    await user.click(within(dialog).getByRole('button', { name: 'Confirm retry' }))
    expect(within(dialog).getByRole('button', { name: 'Working…' })).toBeDisabled()

    const cancelEvent = new Event('cancel', { cancelable: true })
    const accepted = dialog.dispatchEvent(cancelEvent)

    expect(accepted).toBe(false)
    expect(cancelEvent.defaultPrevented).toBe(true)
    expect(dialog).toHaveAttribute('open')

    resolveRetry(fakeResponse({
      ...OUTBOUND_DETAIL,
      status: 'queued',
      delivery: { ...OUTBOUND_DETAIL.delivery, status: 'pending', version: 3 },
    }))
    await waitFor(() => expect(dialog).not.toHaveAttribute('open'))
    expect(queryClient.getQueryState([
      'case-closure-readiness',
      'PRR-2026-0001',
    ])?.isInvalidated).toBe(true)
  })

  it('renders the exact conversation, safe files, attempts, and a liaison handoff', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([INBOUND_DETAIL, OUTBOUND_DETAIL]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([RESPONDED_CYCLE]))
      }
      if (url === '/api/staff/correspondence/inbound-1/?request=PRR-2026-0001') {
        return fakeResponse(INBOUND_DETAIL)
      }
      if (url === '/api/staff/correspondence/outbound-1/?request=PRR-2026-0001') {
        return fakeResponse(OUTBOUND_DETAIL)
      }
      if (url === '/api/staff/correspondence/outbound-1/attempts/') {
        return fakeResponse([
          {
            id: 1,
            attempt_number: 1,
            claim_owner: 'automation-outbound',
            claimed_at: '2026-07-02T12:02:00Z',
            result_at: '2026-07-02T12:05:00Z',
            result_category: 'failed',
            provider_message_id: '',
            provider_thread_id: '',
            error_category: 'provider_unavailable',
            actor: null,
            created_at: '2026-07-02T12:02:00Z',
          },
        ])
      }
      if (url === '/api/staff/correspondence-attachments/attachment-1/download/') {
        return fakeResponse({ url: '#attachment-1', expires_in: 300 })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel(['liaison'])

    expect(await screen.findByText('Case update')).toBeInTheDocument()
    expect(screen.getByText('Re: Case update')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open Re: Case update' }))
    expect(
      within(screen.getByLabelText('Re: Case update')).getByText(
        /Community Advocate <advocate@community\.example\.org> → Public Records <publicrecords@psd401\.net>, Records Officer <officer@psd401\.net>/,
      ),
    ).toBeInTheDocument()
    expect(
      within(screen.getByLabelText('Re: Case update')).queryByText(
        /Requester → District/,
      ),
    ).not.toBeInTheDocument()
    expect(screen.getByText(/Human approval required/)).toBeInTheDocument()
    expect(
      screen.getAllByText(/Clarification response received/).length,
    ).toBeGreaterThan(0)
    const incoming = screen.getByLabelText('Re: Case update')
    expect(within(incoming).getByText('Received at')).toBeInTheDocument()
    expect(within(incoming).getByText('Matched at')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open Case update' }))
    const outgoing = await screen.findByLabelText('Case update')
    expect(within(outgoing).getByText('jane@example.org')).toBeInTheDocument()
    expect(within(outgoing).getByText('context.txt')).toBeInTheDocument()
    expect(within(outgoing).getByText('Failed at')).toBeInTheDocument()
    expect(
      within(screen.getByLabelText('Case update')).getByText(
        'Request status effect: Gathering records',
      ),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Delivery attempts' }))
    expect(await screen.findByText('provider_unavailable')).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: 'Download attachment context.txt' }),
    )
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/staff/correspondence-attachments/attachment-1/download/',
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('labels an inbound message with no parsed sender as unknown', async () => {
    const unknownSender = {
      ...INBOUND_DETAIL,
      id: 'inbound-unknown-sender',
      received_subject: 'Message without a From header',
      received_sender: '',
      received_recipients: ['publicrecords@psd401.net'],
      matched_by: null,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([unknownSender]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (url === '/api/staff/correspondence/inbound-unknown-sender/?request=PRR-2026-0001') {
        return fakeResponse(unknownSender)
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel(['reviewer'])

    await user.click(await screen.findByRole('button', {
      name: 'Open Message without a From header',
    }))
    const incoming = await screen.findByLabelText(
      'Message without a From header',
    )
    expect(
      within(incoming).getByText(
        /Unknown sender → publicrecords@psd401\.net/,
      ),
    ).toBeInTheDocument()
    expect(within(incoming).queryByText(/Requester → District/))
      .not.toBeInTheDocument()
  })

  it('keeps draft and legal mutations hidden from read-only roles', async () => {
    const draft = {
      ...OUTBOUND_DETAIL,
      id: 'draft-1',
      status: 'draft',
      version: 1,
      approved_subject: '',
      approved_body: '',
      delivery: null,
      failed_at: null,
      failure_category: '',
      failure_description: '',
      delivery_status: null,
      attachments: [],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([draft]))
      }
      if (url === '/api/staff/correspondence/draft-1/?request=PRR-2026-0001') {
        return fakeResponse(draft)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([RESPONDED_CYCLE]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel(['reviewer'])

    expect(await screen.findByText('Case update')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Compose message' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit draft' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Delete draft Case update' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve and send' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Resolve clarification' }),
    ).not.toBeInTheDocument()
  })

  it('keeps officer-only legal drafts read-only for a liaison', async () => {
    const closureDraft = {
      ...OUTBOUND_DETAIL,
      id: 'closure-1',
      kind: 'closure',
      status: 'draft',
      version: 1,
      draft_subject: 'Closure review',
      approved_subject: '',
      approved_body: '',
      delivery: null,
      failed_at: null,
      failure_category: '',
      failure_description: '',
      delivery_status: null,
      attachments: [],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([closureDraft]))
      }
      if (url === '/api/staff/correspondence/closure-1/?request=PRR-2026-0001') {
        return fakeResponse(closureDraft)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel(['liaison'])

    expect(await screen.findByText('Closure review')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit draft' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Delete draft Closure review' }),
    ).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Compose message' }))
    const compose = screen.getByRole('heading', { name: 'Compose correspondence' })
      .closest('dialog') as HTMLElement
    expect(
      within(compose).queryByRole('option', { name: 'Closure draft' }),
    ).not.toBeInTheDocument()
    expect(
      within(compose).queryByRole('option', { name: 'Denial draft' }),
    ).not.toBeInTheDocument()
    expect(
      within(compose).queryByRole('option', {
        name: 'No-records / out-of-retention closure',
      }),
    ).not.toBeInTheDocument()
  })

  it('lets a liaison delete an unsent draft after confirming', async () => {
    const draft = {
      ...OUTBOUND_DETAIL,
      id: 'draft-1',
      status: 'draft',
      version: 2,
      approved_subject: '',
      approved_body: '',
      delivery: null,
      failed_at: null,
      failure_category: '',
      failure_description: '',
      delivery_status: null,
      attachments: [],
    }
    let deleted = false
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE(deleted ? [] : [draft]))
      }
      if (url === '/api/staff/correspondence/draft-1/?request=PRR-2026-0001') {
        return fakeResponse(draft)
      }
      if (url === '/api/staff/correspondence/draft-1/delete/' && method === 'POST') {
        deleted = true
        return { ok: true, status: 204, text: async () => '' } as Response
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel(['liaison'])

    await user.click(await screen.findByRole('button', { name: 'Open Case update' }))
    await user.click(
      await screen.findByRole('button', { name: 'Delete draft Case update' }),
    )
    const dialog = screen.getByRole('heading', { name: 'Delete draft' })
      .closest('dialog') as HTMLElement
    expect(fetchMock).not.toHaveBeenCalledWith(
      '/api/staff/correspondence/draft-1/delete/',
      expect.anything(),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Delete draft' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/staff/correspondence/draft-1/delete/',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ expected_version: 2 }),
        }),
      )
    })
    await waitFor(() => {
      expect(screen.queryByText('Case update')).not.toBeInTheDocument()
    })
  })

  it('does not offer cancellation while a delivery is actively claimed', async () => {
    const claimed = {
      ...OUTBOUND_DETAIL,
      id: 'claimed-1',
      status: 'queued',
      failure_category: '',
      failure_description: '',
      failed_at: null,
      delivery_status: 'claimed',
      delivery: {
        ...OUTBOUND_DETAIL.delivery,
        id: 'delivery-claimed-1',
        status: 'claimed',
        claim_expires_at: '2026-07-02T12:10:00Z',
      },
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([claimed]))
      }
      if (url === '/api/staff/correspondence/claimed-1/?request=PRR-2026-0001') {
        return fakeResponse(claimed)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()

    expect(await screen.findByText('Case update')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Cancel Case update' }),
    ).not.toBeInTheDocument()
  })

  it('creates, previews, approves, and queues a general draft', async () => {
    let current: Record<string, unknown> | null = null
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE(current ? [current] : []))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (
        url === '/api/staff/correspondence/drafts/general/'
        && method === 'POST'
      ) {
        const payload = JSON.parse(String(init?.body))
        expect(payload).toMatchObject({
          request_number: 'PRR-2026-0001',
          subject: 'Case update',
          body: 'The search is continuing.',
        })
        current = {
          ...OUTBOUND_DETAIL,
          id: 'draft-1',
          status: 'draft',
          version: 1,
          draft_subject: payload.subject,
          draft_body: `${payload.body}\n`,
          approved_subject: '',
          approved_body: '',
          delivery: null,
          failed_at: null,
          failure_category: '',
          delivery_status: null,
          attachments: [],
        }
        return fakeResponse(current, 201)
      }
      if (url === '/api/staff/correspondence/draft-1/?request=PRR-2026-0001' && method === 'GET') {
        return fakeResponse(current)
      }
      if (url === '/api/staff/correspondence/draft-1/preview/') {
        return fakeResponse({
          correspondence_id: 'draft-1',
          status: 'draft',
          version: 1,
          subject: 'Case update',
          body: 'The search is continuing.\n',
          recipients: OUTBOUND_DETAIL.recipients,
          attachments: OUTBOUND_DETAIL.attachments,
        })
      }
      if (
        url === '/api/staff/correspondence/draft-1/approve-and-send/'
        && method === 'POST'
      ) {
        current = {
          ...current,
          status: 'queued',
          version: 3,
          approved_subject: 'Case update',
          approved_body: 'The search is continuing.\n',
          delivery_status: 'pending',
          delivery: {
            ...OUTBOUND_DETAIL.delivery,
            id: 'delivery-draft-1',
            status: 'pending',
            version: 1,
          },
        }
        return fakeResponse(current)
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel()

    await screen.findByText('No messages match these filters.')
    await user.click(screen.getByRole('button', { name: 'Compose message' }))
    const compose = screen.getByRole('heading', { name: 'Compose correspondence' })
      .closest('dialog') as HTMLElement
    await user.selectOptions(within(compose).getByLabelText('Message type'), 'general')
    await user.type(within(compose).getByLabelText('Subject'), 'Case update')
    await user.type(
      within(compose).getByLabelText('Message'),
      'The search is continuing.',
    )
    await user.click(within(compose).getByRole('button', { name: 'Create draft' }))

    expect(await screen.findByText('Case update')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open Case update' }))
    await user.click(screen.getByRole('button', { name: 'Preview exact message' }))
    const preview = await screen.findByRole('heading', {
      name: 'Exact delivery preview',
    })
    expect(
      within(preview.closest('dialog') as HTMLElement).getByText(
        'The search is continuing.',
      ),
    ).toBeInTheDocument()
    expect(
      within(preview.closest('dialog') as HTMLElement).getByText(
        'To: Jane Requester <jane@example.org>',
      ),
    ).toBeInTheDocument()
    expect(
      within(preview.closest('dialog') as HTMLElement).getByText('context.txt'),
    ).toBeInTheDocument()
    expect(
      within(preview.closest('dialog') as HTMLElement).getByText('12 B · text/plain'),
    ).toBeInTheDocument()
    // The checksum is still available as integrity evidence, just folded away
    // rather than filling the row with 64 hex characters.
    expect(
      within(preview.closest('dialog') as HTMLElement).getByText(
        `SHA-256 ${'a'.repeat(64)}`,
      ),
    ).toBeInTheDocument()
    expect(
      within(preview.closest('dialog') as HTMLElement).getByText(
        'Scanned — clean · Released from quarantine · Available to download',
      ),
    ).toBeInTheDocument()
    await user.click(
      within(preview.closest('dialog') as HTMLElement).getByRole('button', {
        name: 'Close preview',
      }),
    )

    // One decision, one action: approving and releasing were two buttons
    // gated on the identical Records Officer role, so the second click could
    // never be a second person's.
    await user.click(screen.getByRole('button', { name: 'Approve and send' }))
    const sendConfirmation = await screen.findByRole('heading', {
      name: 'Send this message to the requester',
    })
    const sendDialog = sendConfirmation.closest('dialog') as HTMLElement
    expect(within(sendDialog).getByText('Case update')).toBeInTheDocument()
    // The confirmation still gates the send; nothing leaves on the first click.
    expect(fetchMock).not.toHaveBeenCalledWith(
      '/api/staff/correspondence/draft-1/approve-and-send/',
      expect.objectContaining({ method: 'POST' }),
    )
    await user.click(
      within(sendDialog).getByRole('button', { name: 'Send message' }),
    )
    expect(
      await screen.findByText('Pending delivery', { selector: 'span' }),
    ).toBeInTheDocument()
  })

  it('records immutable adequate-search evidence for a no-records closure', async () => {
    let current: Record<string, unknown> | null = null
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE(current ? [current] : []))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (
        url === '/api/staff/correspondence/drafts/no-records-closure/'
        && method === 'POST'
      ) {
        const payload = JSON.parse(String(init?.body))
        expect(payload).toEqual({
          request_number: 'PRR-2026-0001',
          determination: 'no_responsive_records',
          search_completed_at: '2026-08-01T14:30',
          search_steps: [
            {
              location: 'Google Workspace',
              custodian: 'Transportation Director',
              method: 'Vault term and date-range search',
              date_range: '2025-01-01 through 2025-12-31',
              result: 'No responsive records; no unsearched lead remained',
            },
          ],
          adequacy_reason:
            'Every likely location was searched and every obvious lead was followed.',
          retention_schedule_reference: '',
          incident_hold_checked: false,
          prior_exports_checked: false,
          email_drive_copies_checked: false,
          other_copies_checked: false,
          counsel_approval_reference: 'Counsel memo PRA-2026-18',
        })
        current = {
          ...OUTBOUND_DETAIL,
          id: 'no-records-1',
          kind: 'no_records_closure',
          status: 'draft',
          version: 1,
          draft_subject:
            'Public Records Request PRR-2026-0001 — No Responsive Records',
          draft_body: 'The district completed a reasonable search.\n',
          approved_subject: '',
          approved_body: '',
          delivery: null,
          failed_at: null,
          failure_category: '',
          delivery_status: null,
          attachments: [],
        }
        return fakeResponse(current, 201)
      }
      if (url === '/api/staff/correspondence/no-records-1/?request=PRR-2026-0001') {
        return fakeResponse(current)
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel()
    await screen.findByText('No messages match these filters.')
    await user.click(screen.getByRole('button', { name: 'Compose message' }))
    const compose = screen.getByRole('heading', { name: 'Compose correspondence' })
      .closest('dialog') as HTMLElement
    await user.selectOptions(
      within(compose).getByLabelText('Message type'),
      'no_records_closure',
    )
    await user.type(
      within(compose).getByLabelText('Search completed at'),
      '2026-08-01T14:30',
    )
    await user.type(within(compose).getByLabelText('Location searched'), 'Google Workspace')
    await user.type(within(compose).getByLabelText('Custodian'), 'Transportation Director')
    await user.type(
      within(compose).getByLabelText('Search method and terms'),
      'Vault term and date-range search',
    )
    await user.type(
      within(compose).getByLabelText('Date range'),
      '2025-01-01 through 2025-12-31',
    )
    await user.type(
      within(compose).getByLabelText('Result and leads followed'),
      'No responsive records; no unsearched lead remained',
    )
    await user.type(
      within(compose).getByLabelText(/Why this search was adequate/),
      'Every likely location was searched and every obvious lead was followed.',
    )
    await user.type(
      within(compose).getByLabelText(/District counsel approval reference/),
      'Counsel memo PRA-2026-18',
    )
    await user.click(within(compose).getByRole('button', { name: 'Create draft' }))

    expect(
      await screen.findByText(
        'Public Records Request PRR-2026-0001 — No Responsive Records',
      ),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', {
      name: 'Open Public Records Request PRR-2026-0001 — No Responsive Records',
    }))
    await screen.findByLabelText(
      'Public Records Request PRR-2026-0001 — No Responsive Records',
    )
    expect(screen.queryByRole('button', { name: 'Edit draft' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve and send' })).toBeInTheDocument()
  })

  it('keeps a narrowing option attached to its own fields when another is removed', async () => {
    let posted: Record<string, unknown> | null = null
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (
        url === '/api/staff/correspondence/drafts/scope-clarification/'
        && method === 'POST'
      ) {
        posted = JSON.parse(String(init?.body))
        return fakeResponse({ detail: 'stop here' }, 400)
      }
      if (url === '/api/staff/requests/PRR-2026-0001/vault-counts/') {
        return fakeResponse({
          enabled: true,
          corpus: 'MAIL',
          latest: {
            state: 'complete',
            total_count: 29380,
            completed_at: '2026-09-22T17:05:00Z',
            trigger: 'automatic',
          },
        })
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel()
    await screen.findByText('No messages match these filters.')
    await user.click(screen.getByRole('button', { name: 'Compose message' }))
    const compose = screen.getByRole('heading', { name: 'Compose correspondence' })
      .closest('dialog') as HTMLElement
    await user.selectOptions(
      within(compose).getByLabelText('Message type'),
      'scope_clarification',
    )
    await user.type(within(compose).getByLabelText('Items the search matched'), '12')
    // A number that disagrees with the latest count is flagged, not refused.
    expect(
      await within(compose).findByText(/The latest automatic count found 29,380/),
    ).toBeInTheDocument()
    await user.type(
      within(compose).getByLabelText('What was searched, in plain language'),
      'District email mentioning the bus route.',
    )
    await user.type(within(compose).getByLabelText('Reply requested by'), '2026-10-01')
    const addOption = within(compose).getByRole('button', { name: 'Add a narrowing option' })
    await user.click(addOption)
    await user.click(addOption)
    await user.type(
      within(compose).getByLabelText('Option 1: search terms (comma separated)'),
      'first',
    )
    const second = within(compose).getByLabelText(
      'Option 2: search terms (comma separated)',
    )
    await user.type(second, 'bus route, 2026')

    await user.click(within(compose).getByRole('button', { name: 'Remove option 1' }))

    // The same element now labelled as option 1, still holding its own text.
    expect(second).toBeInTheDocument()
    expect(second).toHaveValue('bus route, 2026')
    expect(
      within(compose).getByLabelText('Option 1: search terms (comma separated)'),
    ).toBe(second)

    await user.click(within(compose).getByRole('button', { name: 'Create draft' }))
    await waitFor(() => expect(posted).not.toBeNull())
    expect(posted).toMatchObject({
      form: 'informational',
      match_count: 12,
      narrowing_options: [{ terms: ['bus route', '2026'], rationale: '' }],
    })
  })

  it('requires a selectable structured exemption basis for denial drafts', async () => {
    const eligible = {
      id: 17,
      citation: 'RCW 42.56.250',
      short_name: 'Examination questions',
      description: 'Protected examination material.',
      active: true,
      requires_second_review: false,
    }
    const secondReview = {
      id: 18,
      citation: '20 U.S.C. § 1232g',
      short_name: 'FERPA education records',
      description: 'Education records.',
      active: true,
      requires_second_review: true,
    }
    let current: Record<string, unknown> | null = null
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE(current ? [current] : []))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (url === '/api/staff/exemptions/') {
        return fakeResponse(PAGE([eligible, secondReview]))
      }
      if (
        url === '/api/staff/correspondence/drafts/denial/'
        && method === 'POST'
      ) {
        const payload = JSON.parse(String(init?.body))
        expect(payload.exemption_citation_ids).toEqual([17])
        current = {
          ...OUTBOUND_DETAIL,
          id: 'denial-draft-1',
          kind: 'denial',
          status: 'draft',
          version: 1,
          draft_subject: payload.subject,
          draft_body: `${payload.body}\n`,
          approved_subject: '',
          approved_body: '',
          delivery: null,
          failed_at: null,
          failure_category: '',
          delivery_status: null,
          attachments: [],
          denial_exemption_citations: [eligible],
        }
        return fakeResponse(current, 201)
      }
      if (url === '/api/staff/correspondence/denial-draft-1/?request=PRR-2026-0001') {
        return fakeResponse(current)
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel()
    await screen.findByText('No messages match these filters.')
    await user.click(screen.getByRole('button', { name: 'Compose message' }))
    const compose = screen.getByRole('heading', { name: 'Compose correspondence' })
      .closest('dialog') as HTMLElement
    await user.selectOptions(
      within(compose).getByLabelText('Message type'),
      'denial',
    )
    await within(compose).findByText('RCW 42.56.250')
    const eligibleBox = within(compose).getByRole('checkbox', {
      name: /RCW 42\.56\.250 — Examination questions/,
    })
    const secondReviewBox = within(compose).getByRole('checkbox', {
      name: /20 U\.S\.C\. § 1232g — FERPA education records/,
    })
    expect(secondReviewBox).toBeDisabled()
    await user.click(eligibleBox)
    await user.type(
      within(compose).getByLabelText('Subject'),
      'Denial determination',
    )
    await user.type(
      within(compose).getByLabelText('Message'),
      'The requested records are fully exempt from disclosure.',
    )
    await user.click(
      within(compose).getByRole('button', { name: 'Create draft' }),
    )

    expect(await screen.findByText('Denial determination')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open Denial determination' }))
    await screen.findByLabelText('Denial determination')
    expect(screen.getByText(/RCW 42\.56\.250 — Examination questions/))
      .toBeInTheDocument()
  })

  it('lets an officer remove selected ineligible denial citations', async () => {
    const inactive = {
      id: 21,
      citation: 'RCW 42.56.250',
      short_name: 'Inactive authority',
      description: '',
      active: false,
      requires_second_review: false,
    }
    const secondReview = {
      id: 22,
      citation: '20 U.S.C. § 1232g',
      short_name: 'FERPA education records',
      description: '',
      active: true,
      requires_second_review: true,
    }
    const denialDraft = {
      ...OUTBOUND_DETAIL,
      id: 'denial-edit-1',
      kind: 'denial',
      status: 'draft',
      version: 1,
      draft_subject: 'Denial determination',
      approved_subject: '',
      approved_body: '',
      delivery: null,
      failed_at: null,
      failure_category: '',
      delivery_status: null,
      attachments: [],
      denial_exemption_citations: [inactive, secondReview],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([denialDraft]))
      }
      if (url === '/api/staff/correspondence/denial-edit-1/?request=PRR-2026-0001') {
        return fakeResponse(denialDraft)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (url === '/api/staff/exemptions/') {
        return fakeResponse(PAGE([secondReview]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel()
    await screen.findByText('Denial determination')
    await user.click(screen.getByRole('button', { name: 'Open Denial determination' }))
    await screen.findByLabelText('Denial determination')
    await user.click(screen.getByRole('button', { name: 'Edit draft' }))
    const edit = screen.getByRole('heading', {
      name: 'Edit correspondence draft',
    }).closest('dialog') as HTMLElement
    await within(edit).findByText('RCW 42.56.250')
    const secondReviewBox = within(edit).getByRole('checkbox', {
      name: /20 U\.S\.C\. § 1232g — FERPA education records/,
    })
    const inactiveBox = within(edit).getByRole('checkbox', {
      name: /RCW 42\.56\.250 — Inactive authority/,
    })
    expect(inactiveBox).toBeChecked()
    expect(inactiveBox).toBeEnabled()
    expect(secondReviewBox).toBeChecked()
    expect(secondReviewBox).toBeEnabled()
    await user.click(inactiveBox)
    await user.click(secondReviewBox)
    expect(inactiveBox).not.toBeChecked()
    expect(secondReviewBox).not.toBeChecked()
  })

  it('edits structured clarification inputs through canonical rerendering', async () => {
    let current = {
      ...OUTBOUND_DETAIL,
      ...BASE_SUMMARY,
      id: 'clarification-draft-1',
      direction: 'outbound',
      kind: 'clarification_request',
      status: 'draft',
      created_at: '2026-07-02T12:00:00Z',
      version: 1,
      draft_subject: 'Public Records Request PRR-2026-0001 — Clarification Needed',
      draft_body: 'Please identify the employee names and date range requested.\n',
      draft_metadata: {
        clarification_questions:
          'Please identify the employee names and date range requested.',
        clarification_due: '2026-08-10',
      },
      approved_subject: '',
      approved_body: '',
      approved_metadata: {},
      template_identifier: 'clarification_request',
      template_version: '1',
      failed_at: null,
      failure_category: '',
      failure_description: '',
      delivery_status: null,
      attachments: [],
      delivery: null,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([current]))
      }
      if (url === '/api/staff/correspondence/clarification-draft-1/?request=PRR-2026-0001' && method === 'GET') {
        return fakeResponse(current)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (url === '/api/staff/correspondence/clarification-draft-1/' && method === 'PATCH') {
        const payload = JSON.parse(String(init?.body))
        expect(payload).toEqual({
          expected_version: 1,
          clarification_questions:
            'Please identify the departments and precise date range.',
          clarification_due: '2026-08-14',
          recipients: [
            {
              recipient_type: 'to',
              email: 'jane@example.org',
              display_name: '',
            },
          ],
        })
        current = {
          ...current,
          version: 2,
          draft_body:
            'Please identify the departments and precise date range. Respond by August 14, 2026.\n',
          draft_metadata: {
            clarification_questions:
              'Please identify the departments and precise date range.',
            clarification_due: '2026-08-14',
          },
        }
        return fakeResponse(current)
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel(['liaison'])

    await screen.findByText(
      'Public Records Request PRR-2026-0001 — Clarification Needed',
    )
    await user.click(screen.getByRole('button', {
      name: 'Open Public Records Request PRR-2026-0001 — Clarification Needed',
    }))
    await screen.findByLabelText(
      'Public Records Request PRR-2026-0001 — Clarification Needed',
    )
    await user.click(screen.getByRole('button', { name: 'Edit draft' }))
    const edit = screen.getByRole('heading', {
      name: 'Edit correspondence draft',
    }).closest('dialog') as HTMLElement
    const questions = within(edit).getByLabelText('Clarification questions')
    await user.clear(questions)
    await user.type(
      questions,
      'Please identify the departments and precise date range.',
    )
    const due = within(edit).getByLabelText('Response requested by')
    await user.clear(due)
    await user.type(due, '2026-08-14')
    await user.click(within(edit).getByRole('button', { name: 'Save draft' }))

    expect(
      (await screen.findAllByText(/departments and precise date range/)).length,
    ).toBeGreaterThan(0)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/correspondence/clarification-draft-1/',
      expect.objectContaining({ method: 'PATCH' }),
    )
  })

  it('never tells the officer a third-party notice goes to the requester', async () => {
    const notice = {
      ...OUTBOUND_DETAIL,
      ...BASE_SUMMARY,
      id: 'notice-draft-1',
      direction: 'outbound',
      kind: 'third_party_notice',
      status: 'draft',
      created_at: '2026-07-02T12:00:00Z',
      version: 1,
      draft_subject: 'Notice of a public records request for your records',
      draft_body: 'The District intends to release records about you.\n',
      draft_metadata: {},
      approved_subject: '',
      approved_body: '',
      approved_metadata: {},
      template_identifier: 'third_party_notice',
      template_version: '1',
      failed_at: null,
      failure_category: '',
      failure_description: '',
      delivery_status: null,
      attachments: [],
      delivery: null,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([notice]))
      }
      if (url === '/api/staff/correspondence/notice-draft-1/?request=PRR-2026-0001') {
        return fakeResponse(notice)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    const user = userEvent.setup()
    renderPanel()

    await user.click(await screen.findByRole('button', {
      name: 'Open Notice of a public records request for your records',
    }))
    await user.click(await screen.findByRole('button', { name: 'Approve and send' }))

    const heading = await screen.findByRole('heading', {
      name: 'Send this notice to the person it names',
    })
    const dialog = heading.closest('dialog') as HTMLElement
    expect(within(dialog).queryByText(/goes to the requester/)).not.toBeInTheDocument()
    expect(within(dialog).getByText(/not to the requester/)).toBeInTheDocument()
  })

  it('allows a verified released installment notice to enter approval', async () => {
    const installmentDraft = {
      ...OUTBOUND_DETAIL,
      ...BASE_SUMMARY,
      id: 'installment-draft-1',
      direction: 'outbound',
      kind: 'installment_notice',
      status: 'draft',
      created_at: '2026-07-02T12:00:00Z',
      version: 1,
      draft_subject:
        'Public Records Request PRR-2026-0001 — Installment 1 Available',
      draft_body: 'The verified installment is now available.\n',
      draft_metadata: {
        installment_id: 1,
        download_url:
          'https://prr.psd401.net/requests/PRR-2026-0001/installments/1/download/opaque-token/',
        download_expires_at: '2026-08-09T20:00:00Z',
        package_sha256: 'a'.repeat(64),
        item_count: 1,
        has_exemptions: false,
        is_final: false,
      },
      approved_subject: '',
      approved_body: '',
      approved_metadata: {},
      template_identifier: 'installment_notice',
      template_version: '3',
      failed_at: null,
      failure_category: '',
      failure_description: '',
      delivery_status: null,
      attachments: [],
      delivery: null,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([installmentDraft]))
      }
      if (url === '/api/staff/correspondence/installment-draft-1/?request=PRR-2026-0001') {
        return fakeResponse(installmentDraft)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel()

    await user.click(await screen.findByRole('button', {
      name: 'Open Public Records Request PRR-2026-0001 — Installment 1 Available',
    }))
    expect(
      (await screen.findAllByText(/verified installment is now available/i)).length,
    ).toBeGreaterThan(0)
    expect(
      screen.getByRole('button', { name: 'Approve and send' }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/foundation only/i)).not.toBeInTheDocument()
  })

  it('offers retry, cancellation, and deliberate clarification resolution', async () => {
    const approved = {
      ...OUTBOUND_DETAIL,
      ...BASE_SUMMARY,
      id: 'approved-1',
      direction: 'outbound',
      kind: 'general',
      status: 'approved',
      created_at: '2026-07-01T12:00:00Z',
      version: 3,
      draft_subject: 'Approved update',
      approved_subject: 'Approved update',
      approved_body: 'Approved body.\n',
      attachments: [],
      delivery: null,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([OUTBOUND_DETAIL, approved]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([RESPONDED_CYCLE]))
      }
      if (url === '/api/staff/correspondence/outbound-1/?request=PRR-2026-0001' && method === 'GET') {
        return fakeResponse(OUTBOUND_DETAIL)
      }
      if (url === '/api/staff/correspondence/approved-1/?request=PRR-2026-0001' && method === 'GET') {
        return fakeResponse(approved)
      }
      if (
        url === '/api/staff/clarification-cycles/cycle-1/'
        && method === 'GET'
      ) {
        return fakeResponse(RESPONDED_CYCLE)
      }
      if (url.endsWith('/retry/') && method === 'POST') {
        return fakeResponse({
          ...OUTBOUND_DETAIL,
          status: 'queued',
          delivery: { ...OUTBOUND_DETAIL.delivery, status: 'pending', version: 3 },
        })
      }
      if (url.endsWith('/cancel/') && method === 'POST') {
        return fakeResponse({ ...approved, status: 'cancelled', version: 4 })
      }
      if (url.endsWith('/resolve/') && method === 'POST') {
        return fakeResponse({
          ...RESPONDED_CYCLE,
          resolved_at: '2026-07-04T12:00:00Z',
          resolved_by: 'officer',
          resolution_notes: 'Scope confirmed for school years 2024 through 2026.',
          opened_status_event_id: 1,
          resolved_status_event_id: 2,
        })
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderPanel()

    await screen.findByText('Approved update')
    await user.click(screen.getByRole('button', { name: 'Open Case update' }))
    await screen.findByLabelText('Case update')
    await user.click(screen.getByRole('button', { name: 'Retry Case update' }))
    const retryConfirmation = await screen.findByRole('heading', {
      name: 'Retry correspondence delivery',
    })
    const retryDialog = retryConfirmation.closest('dialog') as HTMLElement
    expect(within(retryDialog).getByText('Case update')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalledWith(
      '/api/staff/correspondence/outbound-1/retry/',
      expect.objectContaining({ method: 'POST' }),
    )
    await user.click(
      within(retryDialog).getByRole('button', { name: 'Confirm retry' }),
    )
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/staff/correspondence/outbound-1/retry/',
        expect.objectContaining({ method: 'POST' }),
      )
    })

    await user.click(screen.getByRole('button', { name: 'Open Approved update' }))
    await screen.findByLabelText('Approved update')
    await user.click(screen.getByRole('button', { name: 'Cancel Approved update' }))
    const cancel = screen.getByRole('heading', { name: 'Cancel correspondence' })
      .closest('dialog') as HTMLElement
    await user.type(
      within(cancel).getByLabelText('Cancellation reason'),
      'Superseded by a corrected draft.',
    )
    await user.click(
      within(cancel).getByRole('button', { name: 'Confirm cancellation' }),
    )

    await user.click(screen.getByRole('button', { name: 'Resolve clarification' }))
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/staff/clarification-cycles/cycle-1/',
        expect.anything(),
      )
    })
    const resolve = screen.getByRole('heading', { name: 'Resolve clarification' })
      .closest('dialog') as HTMLElement
    await user.type(
      within(resolve).getByLabelText('Resolution notes'),
      'Scope confirmed for school years 2024 through 2026.',
    )
    await user.click(
      within(resolve).getByRole('button', { name: 'Resolve cycle' }),
    )

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/staff/clarification-cycles/cycle-1/resolve/',
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })
  it('drafts the courtesy submission receipt without authored content', async () => {
    let current: Record<string, unknown> | null = null
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE(current ? [current] : []))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (
        url === '/api/staff/correspondence/drafts/submission-receipt/'
        && method === 'POST'
      ) {
        expect(JSON.parse(String(init?.body))).toEqual({
          request_number: 'PRR-2026-0001',
        })
        current = {
          ...OUTBOUND_DETAIL,
          id: 'receipt-1',
          kind: 'submission_receipt',
          status: 'draft',
          version: 1,
          draft_subject: 'PRR-2026-0001 — Submission Received',
          draft_body: 'Not the five-business-day response.\n',
          approved_subject: '',
          approved_body: '',
          delivery: null,
          delivery_status: null,
          attachments: [],
        }
        return fakeResponse(current, 201)
      }
      if (url === '/api/staff/correspondence/receipt-1/?request=PRR-2026-0001') {
        return fakeResponse(current)
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderPanel()

    await user.click(await screen.findByRole('button', { name: 'Compose message' }))
    await user.selectOptions(
      screen.getByLabelText('Message type'),
      'submission_receipt',
    )
    expect(screen.queryByLabelText('Subject')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create draft' }))

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/correspondence/drafts/submission-receipt/',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('fills an authored draft from the district starting point, still editable', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse(MESSAGE_PAGE([]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (url.startsWith('/api/staff/correspondence/template-prefill/')) {
        expect(url).toContain('request_number=PRR-2026-0001')
        expect(url).toContain('kind=general')
        return fakeResponse({
          subject: 'Public Records Request PRR-2026-0001',
          body: 'Dear Jane Public,\n\n[Write the message.]\n',
        })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderPanel()

    await user.click(await screen.findByRole('button', { name: 'Compose message' }))
    await user.click(
      screen.getByRole('button', { name: 'Start from district template' }),
    )

    const subject = await screen.findByLabelText('Subject')
    expect(subject).toHaveValue('Public Records Request PRR-2026-0001')
    const body = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(body.value).toContain('[Write the message.]')
    await user.clear(subject)
    await user.type(subject, 'Officer wrote this instead')
    expect(subject).toHaveValue('Officer wrote this instead')
  })
})

describe('CorrespondencePanel — paginated exemption citations', () => {
  it('offers active exemption citations from later pages in the denial selector', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/')) return fakeResponse(pageOf([]))
      if (url.startsWith('/api/staff/clarification-cycles/')) return fakeResponse(pageOf([]))
      if (url.includes('/api/staff/exemptions/') && url.includes('page=2')) {
        return fakeResponse(pageOf([
          {
            id: 2,
            citation: 'RCW 42.56.230(1)',
            short_name: 'Page two citation',
            description: 'A citation returned after the first API page.',
            requires_second_review: false,
            active: true,
          },
        ], { count: 2, previous: '/api/staff/exemptions/' }))
      }
      if (url === '/api/staff/exemptions/') {
        return fakeResponse(pageOf([
          {
            id: 1,
            citation: 'RCW 42.56.210(1)',
            short_name: 'Page one citation',
            description: 'A citation on the first API page.',
            requires_second_review: false,
            active: true,
          },
        ], {
          count: 2,
          next: 'http://localhost/api/staff/exemptions/?page=2',
        }))
      }
      throw new Error(`Unexpected request: ${url}`)
    }))
    const user = userEvent.setup()

    renderHarness(
      <CorrespondencePanel
        requestNumber="PRR-2026-0001"
        roles={['records_officer']}
        requesterEmail="requester@example.org"
      />,
    )

    await user.click(await screen.findByRole('button', { name: 'Compose message' }))
    await user.selectOptions(screen.getByLabelText('Message type'), 'denial')

    expect(await screen.findByText(/Page two citation/)).toBeInTheDocument()
  })
})

describe('CorrespondencePanel requester correction', () => {
  function stubCorrection(result: unknown, status = 200) {
    const calls: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, init })
      if (url.startsWith('/api/staff/correspondence/?request=PRR-2026-0001')) {
        return fakeResponse(MESSAGE_PAGE([]))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0001') {
        return fakeResponse(PAGE([]))
      }
      if (url === '/api/staff/requests/PRR-2026-0001/correct-requester/') {
        return fakeResponse(result, status)
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))
    return calls
  }

  const SUCCESS = {
    previous_requester: {
      name: 'District Info Mailbox',
      email: 'jane@example.org',
    },
    requester: { name: 'Ana Ruiz', email: 'ana@example.org' },
    refreshed_correspondence: [
      {
        id: 'draft-1',
        kind: 'acknowledgment',
        status: 'draft',
        subject: 'Acknowledgment of your request',
      },
    ],
    correspondence_needing_review: [],
    cancelled_correspondence: [
      {
        id: 'approved-1',
        kind: 'acknowledgment',
        status: 'cancelled',
        subject: 'Acknowledgment sent to the wrong address',
      },
    ],
    uncancelled_correspondence: [],
  }

  it('shows the requester on file only to a Records Officer', async () => {
    stubCorrection(SUCCESS)

    const { unmount } = renderPanel(['records_officer'])
    expect(
      await screen.findByRole('button', { name: 'Correct requester' }),
    ).toBeVisible()
    unmount()

    stubCorrection(SUCCESS)
    renderPanel(['liaison'])
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Correct requester' }),
      ).toBeNull()
    })
  })

  it('refuses to submit until the reason is substantive', async () => {
    stubCorrection(SUCCESS)
    const user = userEvent.setup()

    renderPanel(['records_officer'])
    await user.click(
      await screen.findByRole('button', { name: 'Correct requester' }),
    )
    const dialog = await screen.findByRole('dialog', {
      name: 'Correct requester',
    })
    await user.clear(within(dialog).getByLabelText('Requester name'))
    await user.type(within(dialog).getByLabelText('Requester name'), 'Ana Ruiz')
    await user.type(
      within(dialog).getByLabelText(/Reason for the correction/),
      'typo',
    )

    expect(
      within(dialog).getByRole('button', { name: 'Correct requester' }),
    ).toBeDisabled()
    expect(within(dialog).getByText(/more characters needed/)).toBeVisible()
  })

  it('sends the correction and reports what it changed', async () => {
    const calls = stubCorrection(SUCCESS)
    const user = userEvent.setup()

    renderPanel(['records_officer'])
    await user.click(
      await screen.findByRole('button', { name: 'Correct requester' }),
    )
    const dialog = await screen.findByRole('dialog', {
      name: 'Correct requester',
    })
    const name = within(dialog).getByLabelText('Requester name')
    await user.clear(name)
    await user.type(name, 'Ana Ruiz')
    const email = within(dialog).getByLabelText('Requester email')
    await user.clear(email)
    await user.type(email, 'ana@example.org')
    await user.type(
      within(dialog).getByLabelText(/Reason for the correction/),
      'Forwarded by the info mailbox; the requester is named in the thread.',
    )
    await user.click(
      within(dialog).getByRole('button', { name: 'Correct requester' }),
    )

    expect(
      await screen.findByText(
        /Drafts now addressing the corrected requester/,
      ),
    ).toBeVisible()
    expect(
      screen.getByText(/Acknowledgment sent to the wrong address/),
    ).toBeVisible()
    const posted = calls.find(
      (call) => call.url === '/api/staff/requests/PRR-2026-0001/correct-requester/',
    )
    expect(posted?.init?.method).toBe('POST')
    expect(JSON.parse(String(posted?.init?.body))).toEqual({
      name: 'Ana Ruiz',
      email: 'ana@example.org',
      reason:
        'Forwarded by the info mailbox; the requester is named in the thread.',
    })
  })

  it('drops the cached detail of every message the correction touched', async () => {
    stubCorrection(SUCCESS)
    const user = userEvent.setup()

    const { queryClient } = renderPanel(['records_officer'])
    const cancelledKey = correspondenceDetailQueryKey('PRR-2026-0001', 'approved-1')
    const refreshedKey = correspondenceDetailQueryKey('PRR-2026-0001', 'draft-1')
    queryClient.setQueryData(cancelledKey, { id: 'approved-1', status: 'approved' })
    queryClient.setQueryData(refreshedKey, { id: 'draft-1', status: 'draft' })

    await user.click(
      await screen.findByRole('button', { name: 'Correct requester' }),
    )
    const dialog = await screen.findByRole('dialog', {
      name: 'Correct requester',
    })
    await user.type(
      within(dialog).getByLabelText(/Reason for the correction/),
      'Forwarded by the info mailbox; the requester is named in the thread.',
    )
    await user.click(
      within(dialog).getByRole('button', { name: 'Correct requester' }),
    )

    expect(
      await screen.findByText(/Drafts now addressing the corrected requester/),
    ).toBeVisible()
    expect(queryClient.getQueryState(cancelledKey)?.isInvalidated).toBe(true)
    expect(queryClient.getQueryState(refreshedKey)?.isInvalidated).toBe(true)
  })

  it('surfaces a refusal instead of claiming the record changed', async () => {
    stubCorrection(
      { code: 'invalid_requester_correction', detail: ['Nothing would change.'] },
      400,
    )
    const user = userEvent.setup()

    renderPanel(['records_officer'])
    await user.click(
      await screen.findByRole('button', { name: 'Correct requester' }),
    )
    const dialog = await screen.findByRole('dialog', {
      name: 'Correct requester',
    })
    await user.type(
      within(dialog).getByLabelText(/Reason for the correction/),
      'Forwarded by the info mailbox; the requester is named in the thread.',
    )
    await user.click(
      within(dialog).getByRole('button', { name: 'Correct requester' }),
    )

    expect(await within(dialog).findByText(/Nothing would change/)).toBeVisible()
    expect(
      screen.queryByText(/Drafts now addressing the corrected requester/),
    ).toBeNull()
  })
})
