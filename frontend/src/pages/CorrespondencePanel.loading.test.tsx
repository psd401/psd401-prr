// ABOUTME: Regression reproductions for request-scoped correspondence loading.
// ABOUTME: Conversation rendering must avoid N+1 detail fetches and false empty states.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { CorrespondencePanel } from './CorrespondencePanel'

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

function correspondence(id: string, subject: string) {
  return {
    id,
    request_number: 'PRR-2026-0042',
    direction: 'outbound',
    kind: 'general',
    status: 'sent',
    automated: false,
    created_at: '2026-07-30T12:00:00Z',
    approved_at: '2026-07-30T12:01:00Z',
    queued_at: '2026-07-30T12:02:00Z',
    sent_at: '2026-07-30T12:03:00Z',
    received_at: null,
    matched_at: null,
    failed_at: null,
    cancelled_at: null,
    failure_category: '',
    version: 3,
    recipient_count: 1,
    attachment_count: 0,
    delivery_status: 'sent',
    resulting_status_event_id: null,
    inbound_receipt_id: null,
    draft_subject: subject,
    draft_body: `${subject} body.\n`,
    draft_metadata: {},
    approved_subject: subject,
    approved_body: `${subject} body.\n`,
    approved_metadata: {},
    received_subject: '',
    received_body: '',
    received_sender: '',
    received_recipients: [],
    template_identifier: '',
    template_version: '',
    created_by: 'officer',
    approved_by: 'officer',
    queued_by: 'officer',
    matched_by: null,
    rfc_message_id: `<${id}@prr.psd401.net>`,
    provider: 'gmail',
    provider_account: 'records@psd401.net',
    provider_message_id: `gmail-${id}`,
    provider_thread_id: 'thread-42',
    failure_description: '',
    replaces_id: null,
    resulting_status: null,
    recipients: [{
      id: 1,
      recipient_type: 'to',
      email: 'requester@example.org',
      display_name: 'Requester',
      requester_relationship: true,
      created_at: '2026-07-30T12:00:00Z',
    }],
    attachments: [],
    denial_exemption_citations: [],
    delivery: null,
  }
}

function summary(message: ReturnType<typeof correspondence>) {
  return {
    id: message.id,
    request_number: message.request_number,
    direction: message.direction,
    kind: message.kind,
    status: message.status,
    subject: message.approved_subject,
    snippet: message.approved_body,
    automated: message.automated,
    created_at: message.created_at,
    approved_at: message.approved_at,
    queued_at: message.queued_at,
    sent_at: message.sent_at,
    received_at: message.received_at,
    matched_at: message.matched_at,
    failed_at: message.failed_at,
    cancelled_at: message.cancelled_at,
    failure_category: message.failure_category,
    version: message.version,
    recipient_count: message.recipient_count,
    attachment_count: message.attachment_count,
    delivery_status: message.delivery_status,
    matching_status: null,
    resulting_status_event_id: message.resulting_status_event_id,
    inbound_receipt_id: message.inbound_receipt_id,
    work_item: null,
  }
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <CorrespondencePanel
            requestNumber="PRR-2026-0042"
            requesterEmail="requester@example.org"
            roles={['records_officer']}
          />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('CorrespondencePanel — conversation loading', () => {
  it('renders a complete request conversation without one detail request per list row', async () => {
    const messages = [
      correspondence('message-one', 'First case update'),
      correspondence('message-two', 'Second case update'),
    ]
    const requestedPaths: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requestedPaths.push(url)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0042') {
        return response(page(messages.map(summary)))
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0042') {
        return response(page([]))
      }
      if (url === '/api/staff/correspondence/message-one/') {
        return response(messages[0])
      }
      if (url === '/api/staff/correspondence/message-two/') {
        return response(messages[1])
      }
      if (url.includes('correspondence-details')) return response(messages)
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPanel()
    expect(await screen.findByText('First case update')).toBeInTheDocument()
    expect(screen.getByText('Second case update')).toBeInTheDocument()

    const individualDetailRequests = requestedPaths.filter((path) =>
      /^\/api\/staff\/correspondence\/message-(one|two)\/$/.test(path),
    )
    expect(individualDetailRequests).toEqual([])
  })

  it('shows a detail-loading error without claiming that the conversation is empty', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0042') {
        return response({ detail: 'Correspondence detail unavailable.' }, 503)
      }
      if (url === '/api/staff/clarification-cycles/?request=PRR-2026-0042') {
        return response(page([]))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))

    renderPanel()
    const error = await screen.findByText('Correspondence detail unavailable.')
    expect(error).toHaveAttribute('role', 'alert')
    await waitFor(() => {
      expect(
        screen.queryByText('No correspondence recorded for this request.'),
      ).not.toBeInTheDocument()
    })
  })
})
