// ABOUTME: Inbound requester-message queue and manual association UI coverage.
// ABOUTME: Verifies safe queue metadata, least-privilege detail, and match/rematch.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { renderPage as renderHarness } from '../test/pageHarness'
import { InboundCorrespondenceReview } from './InboundCorrespondenceReview'

function fakeResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function page<T>(
  results: T[],
  overrides: Partial<{
    count: number
    next: string | null
    previous: string | null
  }> = {},
) {
  return {
    count: results.length,
    next: null,
    previous: null,
    results,
    ...overrides,
  }
}

function summary(overrides: Record<string, unknown> = {}) {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    correspondence_id: '22222222-2222-4222-8222-222222222222',
    parsing_status: 'parsed',
    matching_status: 'candidate',
    match_method: 'subject',
    candidate_request_number: 'PRR-2026-0042',
    matched_request_number: null,
    quarantined: false,
    quarantine_reason_code: '',
    parsing_error_code: '',
    received_at: '2026-07-23T17:00:00Z',
    created_at: '2026-07-23T17:01:00Z',
    completed_at: '2026-07-23T17:02:00Z',
    attachment_count: 1,
    downloadable_attachment_count: 0,
    quarantined_attachment_count: 1,
    version: 3,
    received_sender: null,
    received_subject: null,
    work_item_id: null,
    work_item_version: null,
    ...overrides,
  }
}

function detail(overrides: Record<string, unknown> = {}) {
  return {
    ...summary(),
    provider: 'gmail',
    provider_account: 'records-inbox',
    provider_thread_id: 'thread-42',
    rfc_message_id: '<reply@example.org>',
    delivery_status_original_message_id: '',
    matched_delivery_id: null,
    in_reply_to: '<outbound@prr.psd401.net>',
    references: ['<earlier@prr.psd401.net>'],
    envelope_sender: 'requester@example.org',
    envelope_recipients: ['publicrecords@psd401.net'],
    received_subject: 'Clarification response',
    received_body: 'The date range is January through March.',
    received_sender: 'Requester <requester@example.org>',
    received_recipients: ['publicrecords@psd401.net'],
    received_headers: { date: 'Thu, 23 Jul 2026 10:00:00 -0700' },
    correspondence_status: 'unmatched',
    correspondence_kind: 'general',
    delivery_notifications: [],
    attachments: [
      {
        id: '33333333-3333-4333-8333-333333333333',
        original_filename: 'supporting-record.pdf',
        content_type: 'application/pdf',
        size: 1234,
        scan_status: 'pending',
        quarantined: true,
        quarantine_reason_code: '',
        is_downloadable: false,
        created_at: '2026-07-23T17:01:00Z',
        scanned_at: null,
      },
    ],
    ...overrides,
  }
}

function renderReview(enabled = true) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ToastProvider>
          <InboundCorrespondenceReview enabled={enabled} />
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...view, queryClient }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Inbound correspondence review', () => {
  it('reviews a PII-minimal candidate and submits an accountable manual match', async () => {
    const row = summary()
    const receipt = detail()
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
        ) {
          return fakeResponse(page([row]))
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${row.id}/`
        ) {
          return fakeResponse(receipt)
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${row.id}/match/`
          && options?.method === 'POST'
        ) {
          return fakeResponse({
            ...receipt,
            matching_status: 'matched',
            matched_request_number: 'PRR-2026-0042',
            version: 4,
          })
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()

    expect(await screen.findByText('PRR-2026-0042')).toBeInTheDocument()
    expect(screen.queryByText('Clarification response')).not.toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Unmatched' }),
    ).toHaveAttribute('aria-pressed', 'true')
    expect(
      screen.getByRole('button', { name: 'Quarantined' }),
    ).toHaveAttribute('aria-pressed', 'false')
    await user.click(screen.getByRole('button', {
      name: (
        `Review email received 2026-07-23`
      ),
    }))
    expect(
      screen.getByRole('dialog', { name: 'Review inbound correspondence' }),
    ).toBeInTheDocument()
    expect(await screen.findByText('Clarification response')).toBeInTheDocument()
    expect(
      screen.getByText('The date range is January through March.'),
    ).toBeInTheDocument()
    expect(screen.getByText('supporting-record.pdf')).toBeInTheDocument()
    expect(screen.getByText('Not downloadable')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /download/i })).not.toBeInTheDocument()
    expect(screen.getByText('Matching evidence')).toBeInTheDocument()
    expect(screen.getByText('Match method')).toBeInTheDocument()
    expect(screen.getByText('subject')).toBeInTheDocument()
    expect(screen.getByText('Current request')).toBeInTheDocument()
    expect(screen.getByText('No current match')).toBeInTheDocument()
    expect(screen.getByText('Candidate request')).toBeInTheDocument()
    expect(screen.getAllByText('PRR-2026-0042').length).toBeGreaterThan(1)
    expect(screen.getByText('gmail')).toBeInTheDocument()
    expect(screen.getByText('records-inbox')).toBeInTheDocument()
    expect(screen.getByText('thread-42')).toBeInTheDocument()
    expect(screen.getByText('<reply@example.org>')).toBeInTheDocument()
    expect(screen.getByText('<outbound@prr.psd401.net>')).toBeInTheDocument()
    expect(screen.getByText('<earlier@prr.psd401.net>')).toBeInTheDocument()
    expect(screen.getByText('publicrecords@psd401.net')).toBeInTheDocument()
    expect(
      screen.getByText('Thu, 23 Jul 2026 10:00:00 -0700'),
    ).toBeInTheDocument()

    expect(screen.getByLabelText('Request number')).toHaveValue(
      'PRR-2026-0042',
    )
    await user.type(
      screen.getByLabelText('Substantive reason'),
      'The headers and case history confirm this request association.',
    )
    await user.click(screen.getByRole('button', { name: 'Match message' }))

    await waitFor(() => {
      const matchCall = fetchMock.mock.calls.find(
        ([url]) => String(url).endsWith(`/${row.id}/match/`),
      )
      expect(matchCall).toBeDefined()
      expect(matchCall?.[1]).toMatchObject({ method: 'POST' })
      expect(JSON.parse(String(matchCall?.[1]?.body))).toEqual({
        request_number: 'PRR-2026-0042',
        reason: 'The headers and case history confirm this request association.',
        expected_version: 3,
      })
    })
  })

  it('invalidates every correspondence view affected by a manual association', async () => {
    const row = summary()
    const receipt = detail()
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
        ) {
          return fakeResponse(page([row]))
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${row.id}/`
        ) {
          return fakeResponse(receipt)
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${row.id}/match/`
          && options?.method === 'POST'
        ) {
          return fakeResponse({
            ...receipt,
            matching_status: 'matched',
            matched_request_number: 'PRR-2026-0042',
            version: 4,
          })
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const { queryClient } = renderReview()
    queryClient.setQueryData(['correspondence', 'transport', ''], {
      pages: [
        { results: ['page-one'] },
        { results: ['page-two'] },
        { results: ['page-three'] },
      ],
      pageParams: [null, 'cursor-two', 'cursor-three'],
    })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    await user.click(await screen.findByRole('button', {
      name: (
        `Review email received 2026-07-23`
      ),
    }))
    await user.type(
      screen.getByLabelText('Substantive reason'),
      'The retained evidence confirms this request association.',
    )
    await user.click(screen.getByRole('button', { name: 'Match message' }))

    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['inbound-correspondence-receipts'],
      })
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['correspondence-operations'],
      })
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['correspondence', 'transport'],
      })
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['correspondence-detail'],
      })
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['clarification-cycles', 'open'],
      })
    })
    expect(queryClient.getQueryData(['correspondence', 'transport', ''])).toEqual({
      pages: [{ results: ['page-one'] }],
      pageParams: [null],
    })
  })

  it('explicitly associates a matched fresh-thread reply with an open clarification', async () => {
    const receiptId = '77777777-7777-4777-8777-777777777777'
    const matched = detail({
      id: receiptId,
      matching_status: 'matched',
      match_method: 'manual',
      candidate_request_number: null,
      matched_request_number: 'PRR-2026-0042',
      correspondence_status: 'received',
      correspondence_kind: 'general',
      version: 7,
    })
    const cycleId = '88888888-8888-4888-8888-888888888888'
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
        ) {
          return fakeResponse(page([]))
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${receiptId}/`
        ) {
          return fakeResponse(matched)
        }
        if (
          url
          === (
            '/api/staff/clarification-cycles/'
            + '?request=PRR-2026-0042&state=open'
          )
        ) {
          return fakeResponse(page([{
            id: cycleId,
            request_number: 'PRR-2026-0042',
            outbound_correspondence_id:
              '99999999-9999-4999-8999-999999999999',
            inbound_response_correspondence_id: null,
            opened_at: '2026-07-22T17:00:00Z',
            responded_at: null,
            resolved_at: null,
            cancelled_at: null,
          }]))
        }
        if (
          url
          === (
            `/api/staff/inbound-correspondence-receipts/${receiptId}/`
            + 'associate-clarification/'
          )
          && options?.method === 'POST'
        ) {
          return fakeResponse({
            ...matched,
            correspondence_kind: 'clarification_response',
            version: 8,
          })
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.type(screen.getByLabelText('Receipt ID'), receiptId)
    await user.click(screen.getByRole('button', { name: 'Open receipt' }))

    expect(
      await screen.findByRole('option', {
        name: 'Opened Jul 22, 2026 — awaiting response',
      }),
    ).toHaveValue(cycleId)
    await user.selectOptions(
      screen.getByLabelText('Open clarification cycle'),
      cycleId,
    )
    await user.type(
      screen.getByLabelText('Clarification association reason'),
      'The requester confirmed this message answers the open clarification.',
    )
    await user.click(
      screen.getByRole('button', {
        name: 'Associate clarification response',
      }),
    )

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([url]) => String(url).endsWith('/associate-clarification/'),
      )
      expect(call).toBeDefined()
      expect(call?.[1]).toMatchObject({ method: 'POST' })
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        clarification_cycle_id: cycleId,
        reason: (
          'The requester confirmed this message answers the open clarification.'
        ),
        expected_version: 7,
      })
    })
  })

  it('shows DSN exception evidence without offering match, retry, or legal-state actions', async () => {
    const receiptId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    const dsn = detail({
      id: receiptId,
      matching_status: 'matched',
      match_method: 'delivery_status',
      candidate_request_number: null,
      matched_request_number: 'PRR-2026-0042',
      correspondence_status: 'received',
      correspondence_kind: 'delivery_status_notification',
      delivery_status_original_message_id: '<original@prr.psd401.net>',
      matched_delivery_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
      delivery_notifications: [
        {
          id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
          delivery_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
          recipient_index: 1,
          recipient_match_status: 'unmatched',
          action: 'failed',
          enhanced_status: '5.1.1',
          created_at: '2026-07-24T17:02:00Z',
        },
      ],
    })
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL) => {
        const url = String(input)
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
        ) {
          return fakeResponse(page([]))
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${receiptId}/`
        ) {
          return fakeResponse(dsn)
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.type(screen.getByLabelText('Receipt ID'), receiptId)
    await user.click(screen.getByRole('button', { name: 'Open receipt' }))

    expect(
      await screen.findByText(
        'Unverified delivery status report (advisory)',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('<original@prr.psd401.net>')).toBeInTheDocument()
    expect(
      screen.getByText('cccccccc-cccc-4ccc-8ccc-cccccccccccc'),
    ).toBeInTheDocument()
    expect(screen.getByText('failed')).toBeInTheDocument()
    expect(screen.getByText('5.1.1')).toBeInTheDocument()
    expect(
      screen.getByText(/does not retry delivery or change request status/i),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Match message' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Rematch message' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', {
        name: 'Associate clarification response',
      }),
    ).not.toBeInTheDocument()
  })

  it('shows sanitized quarantine codes and supports correction by receipt id', async () => {
    const quarantined = summary({
      id: '44444444-4444-4444-8444-444444444444',
      correspondence_id: '55555555-5555-4555-8555-555555555555',
      parsing_status: 'quarantined',
      matching_status: 'quarantined',
      match_method: '',
      candidate_request_number: null,
      quarantined: true,
      quarantine_reason_code: 'mime_depth_exceeded',
      parsing_error_code: 'parsing_failed',
    })
    const matchedId = '66666666-6666-4666-8666-666666666666'
    const matched = detail({
      id: matchedId,
      matching_status: 'matched',
      match_method: 'in_reply_to',
      candidate_request_number: null,
      matched_request_number: 'PRR-2026-0042',
      correspondence_status: 'received',
      version: 7,
    })
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
        ) {
          return fakeResponse(page([]))
        }
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=quarantined'
        ) {
          return fakeResponse(page([quarantined]))
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${matchedId}/`
        ) {
          return fakeResponse(matched)
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${matchedId}/rematch/`
          && options?.method === 'POST'
        ) {
          return fakeResponse({
            ...matched,
            matched_request_number: 'PRR-2026-0099',
            version: 8,
          })
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByRole('button', { name: 'Quarantined' }))
    expect(
      screen.getByRole('button', { name: 'Unmatched' }),
    ).toHaveAttribute('aria-pressed', 'false')
    expect(
      screen.getByRole('button', { name: 'Quarantined' }),
    ).toHaveAttribute('aria-pressed', 'true')

    // The codes now sit in the row's small metadata line beside the plain
    // description, rather than being the row's entire content.
    const rows = await screen.findAllByRole('listitem')
    const rowText = rows.map((row) => row.textContent ?? '').join(' ')
    expect(rowText).toContain('mime_depth_exceeded')
    expect(rowText).toContain('parsing_failed')
    // A message the parser could not read is never described as a virus hold.
    expect(rowText).toContain('PRR could not read it, so no case was opened')
    expect(rowText).toContain('Could not read')
    expect(rowText).not.toContain('failed the virus scan')
    await user.type(screen.getByLabelText('Receipt ID'), matchedId)
    await user.click(screen.getByRole('button', { name: 'Open receipt' }))
    expect(await screen.findByText('Clarification response')).toBeInTheDocument()

    await user.clear(screen.getByLabelText('Request number'))
    await user.type(screen.getByLabelText('Request number'), 'PRR-2026-0099')
    await user.type(
      screen.getByLabelText('Substantive reason'),
      'The original forwarded header belongs to a different case record.',
    )
    await user.click(screen.getByRole('button', { name: 'Rematch message' }))

    await waitFor(() => {
      const rematchCall = fetchMock.mock.calls.find(
        ([url]) => String(url).endsWith(`/${matchedId}/rematch/`),
      )
      expect(rematchCall).toBeDefined()
      expect(JSON.parse(String(rematchCall?.[1]?.body))).toEqual({
        request_number: 'PRR-2026-0099',
        reason: 'The original forwarded header belongs to a different case record.',
        expected_version: 7,
      })
    })
  })

  it('opens the existing secure download flow only for a released attachment', async () => {
    const row = summary({
      downloadable_attachment_count: 1,
      quarantined_attachment_count: 0,
    })
    const attachmentId = '33333333-3333-4333-8333-333333333333'
    const receipt = detail({
      attachments: [
        {
          id: attachmentId,
          original_filename: 'released-evidence.pdf',
          content_type: 'application/pdf',
          size: 1234,
          scan_status: 'clean',
          quarantined: false,
          quarantine_reason_code: '',
          is_downloadable: true,
          created_at: '2026-07-23T17:01:00Z',
          scanned_at: '2026-07-23T17:03:00Z',
        },
      ],
    })
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
        ) {
          return fakeResponse(page([row]))
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${row.id}/`
        ) {
          return fakeResponse(receipt)
        }
        if (
          url
          === `/api/staff/correspondence-attachments/${attachmentId}/download/`
          && options?.method === 'POST'
        ) {
          return fakeResponse({
            url: `/api/staff/correspondence-attachments/${attachmentId}/content/token`,
            expires_in: 300,
          })
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByRole('button', {
      name: (
        `Review email received 2026-07-23`
      ),
    }))
    await user.click(
      await screen.findByRole('button', {
        name: 'Download attachment released-evidence.pdf',
      }),
    )

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/staff/correspondence-attachments/${attachmentId}/download/`,
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('shows work aggregated from every DRF page by the shared client', async () => {
    const first = summary({
      id: '77777777-7777-4777-8777-777777777777',
      candidate_request_number: 'PRR-2026-0001',
    })
    const second = summary({
      id: '88888888-8888-4888-8888-888888888888',
      candidate_request_number: 'PRR-2026-0051',
    })
    const pageTwoPath = (
      '/api/staff/inbound-correspondence-receipts/'
      + '?page=2&queue=unmatched'
    )
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (
        url
        === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
      ) {
        return fakeResponse(page([first], {
          count: 51,
          next: `http://localhost${pageTwoPath}`,
        }))
      }
      if (url === `http://localhost${pageTwoPath}`) {
        return fakeResponse(page([second], {
          count: 51,
          previous: (
            'http://localhost/api/staff/'
            + 'inbound-correspondence-receipts/?queue=unmatched'
          ),
        }))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderReview()

    expect(await screen.findByText('PRR-2026-0001')).toBeInTheDocument()
    expect(screen.getByText('PRR-2026-0051')).toBeInTheDocument()
    expect(screen.getByText('Showing 2 of 51 messages')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /page of unmatched/i }))
      .not.toBeInTheDocument()
  })

  it('renders a quarantined receipt with nullable parsed content safely', async () => {
    const row = summary({
      parsing_status: 'quarantined',
      matching_status: 'quarantined',
      correspondence_id: null,
      candidate_request_number: null,
      quarantined: true,
      quarantine_reason_code: 'malformed_message',
      parsing_error_code: 'malformed_message',
      attachment_count: 0,
      quarantined_attachment_count: 0,
    })
    const receipt = detail({
      ...row,
      received_subject: null,
      received_body: null,
      received_sender: null,
      received_recipients: null,
      correspondence_status: null,
      correspondence_kind: null,
      received_headers: {},
      attachments: [],
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (
        url
        === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
      ) {
        return fakeResponse(page([]))
      }
      if (
        url
        === '/api/staff/inbound-correspondence-receipts/?queue=quarantined'
      ) {
        return fakeResponse(page([row]))
      }
      if (
        url
        === `/api/staff/inbound-correspondence-receipts/${row.id}/`
      ) {
        return fakeResponse(receipt)
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByRole('button', { name: 'Quarantined' }))
    await user.click(await screen.findByRole('button', {
      name: (
        `Review email received 2026-07-23`
      ),
    }))

    expect(await screen.findByText('(No subject)')).toBeInTheDocument()
    expect(screen.getByText('(No plain-text body)')).toBeInTheDocument()
    expect(screen.getByText('No attachments.')).toBeInTheDocument()
    expect(screen.getByText(/cannot be matched until parsing/)).toBeInTheDocument()
  })

  it('names the dialog and offers an explicit close after receipt load failure', async () => {
    const row = summary()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (
        url
        === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
      ) {
        return fakeResponse(page([row]))
      }
      if (
        url
        === `/api/staff/inbound-correspondence-receipts/${row.id}/`
      ) {
        return fakeResponse(
          { detail: 'The retained receipt could not be loaded.' },
          503,
        )
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByRole('button', {
      name: (
        `Review email received 2026-07-23`
      ),
    }))

    const dialog = screen.getByRole('dialog', {
      name: 'Review inbound correspondence',
    })
    expect(dialog).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The retained receipt could not be loaded.',
    )
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog', {
        name: 'Review inbound correspondence',
      })).not.toBeInTheDocument()
    })
  })

  it('clears a stale attachment-download error when a receipt is reopened', async () => {
    const row = summary()
    const attachmentId = '33333333-3333-4333-8333-333333333333'
    const receipt = detail({
      attachments: [
        {
          id: attachmentId,
          original_filename: 'released-evidence.pdf',
          content_type: 'application/pdf',
          size: 1234,
          scan_status: 'clean',
          quarantined: false,
          quarantine_reason_code: '',
          is_downloadable: true,
          created_at: '2026-07-23T17:01:00Z',
          scanned_at: '2026-07-23T17:03:00Z',
        },
      ],
    })
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (
          url
          === '/api/staff/inbound-correspondence-receipts/?queue=unmatched'
        ) {
          return fakeResponse(page([row]))
        }
        if (
          url
          === `/api/staff/inbound-correspondence-receipts/${row.id}/`
        ) {
          return fakeResponse(receipt)
        }
        if (
          url
          === `/api/staff/correspondence-attachments/${attachmentId}/download/`
          && options?.method === 'POST'
        ) {
          return fakeResponse({ detail: 'Temporary download failure.' }, 503)
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    const reviewName = (
      `Review email received 2026-07-23`
    )
    await user.click(await screen.findByRole('button', { name: reviewName }))
    await user.click(screen.getByRole('button', {
      name: 'Download attachment released-evidence.pdf',
    }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Temporary download failure.',
    )

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: reviewName }))
    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  it('names the sender and subject on each row', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([summary({
      matching_status: 'unmatched',
      candidate_request_number: null,
      received_sender: 'Verizon Business <communications@b2binfo.example>',
      received_subject: 'Outages can slow down your business',
    })]))))

    renderReview()

    expect(
      await screen.findByText('Outages can slow down your business'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Verizon Business <communications@b2binfo.example>/),
    ).toBeInTheDocument()
  })

  it('records "not a records request" for one row through its work item', async () => {
    const row = summary({
      matching_status: 'unmatched',
      candidate_request_number: null,
      received_sender: 'Verizon Business <communications@b2binfo.example>',
      received_subject: 'Outages can slow down your business',
      work_item_id: '77777777-7777-4777-8777-777777777777',
      work_item_version: 2,
    })
    let listed = [row]
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (url === '/api/staff/inbound-correspondence-receipts/?queue=unmatched') {
          return fakeResponse(page(listed))
        }
        if (
          url === `/api/staff/work-items/${row.work_item_id}/no-action-required/`
          && options?.method === 'POST'
        ) {
          listed = []
          return fakeResponse({ id: row.work_item_id })
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByRole('button', {
      name: 'Not a request: Outages can slow down your business',
    }))
    const dialog = screen.getByRole('dialog', { name: 'Record no action required' })
    expect(dialog).toBeInTheDocument()
    expect(screen.getByLabelText('No-action category')).toHaveValue(
      'not_a_records_request',
    )
    await user.type(
      screen.getByLabelText(/Why no team action is required/),
      'Vendor marketing sent to the former officer address.',
    )
    await user.click(screen.getByRole('button', { name: 'Record no action' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).endsWith('/no-action-required/'))
      expect(call).toBeDefined()
      const body = JSON.parse(String(call?.[1]?.body))
      expect(body).toMatchObject({
        category: 'not_a_records_request',
        reason: 'Vendor marketing sent to the former officer address.',
        expected_version: 2,
      })
      expect(body.idempotency_key).toEqual(expect.any(String))
    })
    expect(
      await screen.findByText('No messages are waiting in this queue.'),
    ).toBeInTheDocument()
  })

  it('dismisses every selected row with one decision', async () => {
    const rows = [
      summary({
        id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        matching_status: 'unmatched',
        candidate_request_number: null,
        received_subject: 'Fog advisory',
        work_item_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        work_item_version: 1,
      }),
      summary({
        id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
        matching_status: 'unmatched',
        candidate_request_number: null,
        received_subject: 'Red flag warning',
        work_item_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
        work_item_version: 4,
      }),
      summary({
        id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
        matching_status: 'unmatched',
        candidate_request_number: null,
        received_subject: 'Kept for review',
        work_item_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
        work_item_version: 1,
      }),
    ]
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        if (url === '/api/staff/inbound-correspondence-receipts/?queue=unmatched') {
          return fakeResponse(page(rows))
        }
        if (url.endsWith('/no-action-required/') && options?.method === 'POST') {
          return fakeResponse({})
        }
        throw new Error(`Unexpected fetch: ${url}`)
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByRole('checkbox', { name: 'Select: Fog advisory' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select: Red flag warning' }))
    await user.click(screen.getByRole('button', { name: 'Dismiss 2 selected' }))
    await user.type(
      screen.getByLabelText(/Why no team action is required/),
      'Everbridge public alerts, not records requests.',
    )
    await user.click(screen.getByRole('button', { name: 'Record no action' }))

    await waitFor(() => {
      const calls = fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith('/no-action-required/'))
      expect(calls.map(([url]) => String(url)).sort()).toEqual([
        '/api/staff/work-items/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/no-action-required/',
        '/api/staff/work-items/dddddddd-dddd-4ddd-8ddd-dddddddddddd/no-action-required/',
      ])
      const versions = calls.map(([, init]) =>
        JSON.parse(String(init?.body)).expected_version).sort()
      expect(versions).toEqual([1, 4])
    })
  })

  it('offers no dismissal for a row without an open work item', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([summary({
      received_subject: 'Not yet reconciled',
    })]))))

    renderReview()

    expect(await screen.findByText('Not yet reconciled')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Not a request/ }),
    ).not.toBeInTheDocument()
  })

  it('does not load or expose the queue to non-officers', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    renderReview(false)

    expect(
      screen.getByText(/Records Officer role is required/),
    ).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('InboundCorrespondenceReview — paginated clarification cycles', () => {
  it('offers open clarification cycles from later pages in the association dropdown', async () => {
    const receiptId = '77777777-7777-4777-8777-777777777777'
    const receipt = {
      id: receiptId,
      correspondence_id: '22222222-2222-4222-8222-222222222222',
      parsing_status: 'parsed',
      matching_status: 'matched',
      match_method: 'manual',
      candidate_request_number: null,
      matched_request_number: 'PRR-2026-0042',
      quarantined: false,
      quarantine_reason_code: '',
      parsing_error_code: '',
      received_at: '2026-07-23T17:00:00Z',
      created_at: '2026-07-23T17:01:00Z',
      completed_at: '2026-07-23T17:02:00Z',
      attachment_count: 0,
      downloadable_attachment_count: 0,
      quarantined_attachment_count: 0,
      version: 7,
      provider: 'gmail',
      provider_account: 'records-inbox',
      provider_thread_id: 'thread-42',
      rfc_message_id: '<reply@example.org>',
      delivery_status_original_message_id: '',
      matched_delivery_id: null,
      in_reply_to: '',
      references: [],
      envelope_sender: 'requester@example.org',
      envelope_recipients: ['publicrecords@psd401.net'],
      received_subject: 'Clarification response',
      received_body: 'A response received on a fresh thread.',
      received_sender: 'Requester <requester@example.org>',
      received_recipients: ['publicrecords@psd401.net'],
      received_headers: {},
      correspondence_status: 'received',
      correspondence_kind: 'general',
      delivery_notifications: [],
      attachments: [],
    }
    const cycle = (id: string, openedAt: string) => ({
      id,
      request_number: 'PRR-2026-0042',
      outbound_correspondence_id: '99999999-9999-4999-8999-999999999999',
      inbound_response_correspondence_id: null,
      opened_at: openedAt,
      responded_at: null,
      resolved_at: null,
      cancelled_at: null,
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/inbound-correspondence-receipts/?queue=unmatched') {
        return fakeResponse(page([]))
      }
      if (url === `/api/staff/inbound-correspondence-receipts/${receiptId}/`) {
        return fakeResponse(receipt)
      }
      if (url.includes('/api/staff/clarification-cycles/') && url.includes('page=2')) {
        return fakeResponse(page([
          cycle('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '2026-07-23T17:00:00Z'),
        ], { count: 2, previous: '/api/staff/clarification-cycles/?page=1' }))
      }
      if (url.startsWith('/api/staff/clarification-cycles/')) {
        return fakeResponse(page([
          cycle('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '2026-07-22T17:00:00Z'),
        ], {
          count: 2,
          next: 'http://localhost/api/staff/clarification-cycles/?request=PRR-2026-0042&state=open&page=2',
        }))
      }
      throw new Error(`Unexpected request: ${url}`)
    }))

    renderHarness(
      <InboundCorrespondenceReview enabled />,
      `/?inbound_receipt=${receiptId}`,
    )

    expect(await screen.findByRole('option', {
      name: 'Opened Jul 23, 2026 — awaiting response',
    })).toBeInTheDocument()
  })
})
