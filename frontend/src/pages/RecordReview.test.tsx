// ABOUTME: Staff review UI uses secure raster previews and the same legal
// ABOUTME: review, redaction, second-review, and burn-in REST primitives.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { RecordItems } from './RecordItems'

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

const baseItem = {
  id: '11111111-1111-4111-8111-111111111111',
  request_number: 'PRR-2026-0042',
  source_system: 'google_vault_gmail',
  custodians: [],
  tags: [],
  email_from: '',
  email_to: '',
  email_cc: '',
  email_sent_at: null,
  email_message_id: '',
  email_in_reply_to: '',
  email_sender_domain: '',
  email_auto_submitted: '',
  email_list_id: '',
  email_precedence: '',
  email_has_calendar_part: false,
  advisory_suggested_state: null,
  advisory_confidence: null,
  custodian: 'records@example.org',
  original_filename: 'student-record.pdf',
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

function renderReview() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter><RecordItems roles={['reviewer']} /></MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('record review workspace', () => {
  it('explains how responsive review unlocks the redaction workflow', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([baseItem]))
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/redactions/')) return response(page([]))
      if (url.startsWith('/api/staff/exemptions/')) return response(page([]))
      if (url.endsWith('/advisory-responsiveness/')) return response({
        enabled: true,
        latest: null,
      })
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))

    expect(await screen.findByText('Redaction workflow')).toBeInTheDocument()
    expect(screen.getByText(/available only after a record is confirmed responsive/i))
      .toBeInTheDocument()
    expect(screen.getByText(/draw a box on the page preview/i)).toBeInTheDocument()
  })

  it('starts with no outcome chosen when there is no AI suggestion', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([baseItem]))
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/redactions/')) return response(page([]))
      if (url.startsWith('/api/staff/exemptions/')) return response(page([]))
      if (url.endsWith('/advisory-responsiveness/')) return response({ enabled: true, latest: null })
      throw new Error(`Unexpected request: ${url}`)
    }))
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))

    // One click must not confirm a judgment nobody made.
    expect(await screen.findByLabelText('Review outcome')).toHaveValue('')
    // Nothing is queued unless automatic proposals are on, so say only what is true.
    expect(screen.getByText('No AI suggestion for this record yet.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm review outcome' })).toBeDisabled()
    await user.selectOptions(screen.getByLabelText('Review outcome'), 'responsive')
    expect(screen.getByRole('button', { name: 'Confirm review outcome' })).toBeEnabled()
  })

  it('preselects the AI responsiveness recommendation for explicit human approval', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([baseItem]))
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/redactions/')) return response(page([]))
      if (url.startsWith('/api/staff/exemptions/')) return response(page([]))
      if (url.endsWith('/advisory-responsiveness/')) return response({
        enabled: true,
        latest: {
          id: 'advisory-1',
          state: 'complete',
          model_identifier: 'approved-model-v1',
          input_sha256: 'd'.repeat(64),
          input_truncated: false,
          applied: false,
          proposal: {
            suggested_state: 'non_responsive',
            confidence: 0.72,
            matched_terms: ['field project'],
            rationale: 'The text appears outside the requested date range.',
          },
          failure_detail: '',
        },
      })
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))

    expect(await screen.findByText('Suggested: non_responsive')).toBeInTheDocument()
    expect(screen.getByText(/permanently non-applying/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Review outcome')).toHaveValue('non_responsive')
    expect(screen.queryByRole('button', { name: 'Run' })).not.toBeInTheDocument()
  })

  it('previews a raster page and records the human review outcome', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([baseItem]))
      if (url === `/api/staff/record-items/${baseItem.id}/preview-metadata/`) {
        return response({ page_count: 2 })
      }
      if (url.startsWith('/api/staff/redactions/')) return response(page([]))
      if (url.startsWith('/api/staff/exemptions/')) return response(page([
        {
          id: 9,
          citation: '20 U.S.C. § 1232g',
          short_name: 'FERPA education records',
          requires_second_review: true,
          active: true,
        },
      ]))
      if (url === `/api/staff/record-items/${baseItem.id}/review/`) {
        expect(init?.method).toBe('POST')
        expect(JSON.parse(String(init?.body))).toEqual({
          state: 'responsive',
          withholding_exemption_ids: [],
        })
        return response({ ...baseItem, review_state: 'responsive', reviewed_by_username: 'reviewer' })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))

    const preview = await screen.findByRole('img', { name: 'Record page 1 of 2' })
    expect(preview).toHaveAttribute(
      'src',
      `/api/staff/record-items/${baseItem.id}/preview-page/?page=1`,
    )
    await user.selectOptions(screen.getByLabelText('Review outcome'), 'responsive')
    await user.click(screen.getByRole('button', { name: 'Confirm review outcome' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/staff/record-items/${baseItem.id}/review/`,
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('draws a normalized exemption-backed redaction region', async () => {
    const responsive = { ...baseItem, review_state: 'responsive', reviewed_by_username: 'reviewer' }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([responsive]))
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/exemptions/')) return response(page([
        {
          id: 9,
          citation: '20 U.S.C. § 1232g',
          short_name: 'FERPA education records',
          requires_second_review: true,
          active: true,
        },
      ]))
      if (url === `/api/staff/redactions/?record_item=${baseItem.id}`) return response(page([]))
      if (url === '/api/staff/redactions/') {
        const body = JSON.parse(String(init?.body))
        expect(body).toMatchObject({
          record_item: baseItem.id,
          page_number: 1,
          exemption: 9,
          x0: 0.1,
          y0: 0.2,
          x1: 0.8,
          y1: 0.7,
        })
        return response({ id: 12, ...body, citation: '20 U.S.C. § 1232g' }, 201)
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))
    const drawingSurface = await screen.findByLabelText('Draw redactions on page 1')
    vi.spyOn(drawingSurface, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 1000,
      bottom: 1000,
      width: 1000,
      height: 1000,
      toJSON: () => ({}),
    })
    fireEvent.pointerDown(drawingSurface, { clientX: 100, clientY: 200, pointerId: 1 })
    fireEvent.pointerMove(drawingSurface, { clientX: 800, clientY: 700, pointerId: 1, buttons: 1 })
    fireEvent.pointerUp(drawingSurface, { clientX: 800, clientY: 700, pointerId: 1, buttons: 1 })
    await user.selectOptions(screen.getByLabelText('Redaction exemption'), '9')
    await user.click(screen.getByRole('button', { name: 'Add redaction' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/staff/redactions/',
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('loads a grounded AI proposal for human editing and explicit acceptance', async () => {
    const responsive = { ...baseItem, review_state: 'responsive', reviewed_by_username: 'reviewer' }
    const proposal = {
      id: '22222222-2222-4222-8222-222222222222',
      advisory_run: '33333333-3333-4333-8333-333333333333',
      record_item: baseItem.id,
      page_number: 1,
      x0: 0.1,
      y0: 0.2,
      x1: 0.4,
      y1: 0.3,
      exemption: 9,
      citation: '20 U.S.C. § 1232g',
      exemption_name: 'FERPA education records',
      requires_second_review: true,
      confidence: 0.91,
      rationale: 'Possible student identifier.',
      source_span_ids: ['p1s1'],
      source_text_sha256: 'b'.repeat(64),
      created_at: '2026-08-01T12:00:00Z',
      decision: null,
    }
    let accepted = false
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([responsive]))
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/exemptions/')) return response(page([
        {
          id: 9,
          citation: '20 U.S.C. § 1232g',
          short_name: 'FERPA education records',
          requires_second_review: true,
          active: true,
        },
      ]))
      if (url === `/api/staff/redactions/?record_item=${baseItem.id}`) return response(page([]))
      if (url.endsWith('/advisory-responsiveness/')) return response({ enabled: true, latest: null })
      if (url.endsWith('/advisory-redactions/')) return response({
        enabled: true,
        latest: {
          id: proposal.advisory_run,
          state: 'complete',
          model_identifier: 'approved-model-v1',
          input_sha256: 'c'.repeat(64),
          input_truncated: false,
          applied: false,
          proposal: { candidate_count: 1, page_count: 1 },
          failure_detail: '',
        },
        proposals: accepted ? [{
          ...proposal,
          decision: {
            outcome: 'accepted',
            decided_by_username: 'reviewer',
            reason: '',
            redaction: 12,
            decided_at: '2026-08-01T13:00:00Z',
          },
        }] : [proposal],
      })
      if (url.endsWith(`/redaction-proposals/${proposal.id}/accept/`)) {
        const body = JSON.parse(String(init?.body))
        expect(body).toEqual({
          page_number: 1,
          x0: 0.1,
          y0: 0.2,
          x1: 0.4,
          y1: 0.3,
          exemption: 9,
          explanation: 'Possible student identifier.',
        })
        accepted = true
        return response({ ...proposal, decision: { outcome: 'accepted', redaction: 12 } })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))
    expect(await screen.findByText('1 candidate needs review')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Review proposal' }))
    expect(screen.getByLabelText('Redaction exemption')).toHaveValue('9')
    expect(screen.getByLabelText(/Explanation/)).toHaveValue('Possible student identifier.')
    await user.click(screen.getByRole('button', { name: 'Accept edited proposal' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/staff/redaction-proposals/${proposal.id}/accept/`,
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('offers distinct second review before immutable burn-in', async () => {
    const gated = {
      ...baseItem,
      review_state: 'responsive',
      reviewed_by_username: 'first-reviewer',
      redaction_count: 1,
      second_review_required: true,
    }
    const redaction = {
      id: 12,
      record_item: baseItem.id,
      page_number: 1,
      x0: 0.1,
      y0: 0.2,
      x1: 0.8,
      y1: 0.7,
      exemption: 9,
      citation: '20 U.S.C. § 1232g',
      exemption_name: 'FERPA education records',
      requires_second_review: true,
      explanation: 'Student identifier',
      created_by_username: 'first-reviewer',
      created_at: '2026-08-01T12:00:00Z',
      voided_at: null,
      void_reason: '',
    }
    let approved = false
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([gated]))
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/exemptions/')) return response(page([]))
      if (url === `/api/staff/redactions/?record_item=${baseItem.id}`) {
        return response(page([redaction]))
      }
      if (url.endsWith('/second-review/')) {
        expect(init?.method).toBe('POST')
        approved = true
        return response({
          ...gated,
          second_reviewed_by_username: 'second-reviewer',
          second_reviewed_at: '2026-08-02T12:00:00Z',
        })
      }
      if (url.endsWith('/render-redactions/')) {
        expect(approved).toBe(true)
        return response({
          ...gated,
          second_reviewed_by_username: 'second-reviewer',
          redacted_artifact_ready: true,
        })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))
    await user.click(await screen.findByRole('button', { name: 'Approve second review' }))
    await user.click(await screen.findByRole('button', { name: 'Build redacted PDF' }))

    expect(await screen.findByText('Redacted PDF ready')).toBeInTheDocument()
  })

  it('keeps an edit made while the opened record is still being fetched', async () => {
    // Opening a row shows the record at once from the list and starts a
    // background fetch of the same record by id. Whatever the reviewer does in
    // that window must survive the fetch landing.
    let resolveDetail: (value: Response) => void = () => {}
    const detail = new Promise<Response>((resolve) => {
      resolveDetail = resolve
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/record-items/') return response(page([baseItem]))
      if (url === `/api/staff/record-items/${baseItem.id}/`) return detail
      if (url.endsWith('/preview-metadata/')) return response({ page_count: 1 })
      if (url.startsWith('/api/staff/redactions/')) return response(page([]))
      if (url.startsWith('/api/staff/exemptions/')) return response(page([]))
      if (url.endsWith('/advisory-responsiveness/')) return response({
        enabled: false,
        latest: null,
      })
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderReview()
    await user.click(await screen.findByText('student-record.pdf'))
    await user.selectOptions(await screen.findByLabelText('Review outcome'), 'non_responsive')
    expect(screen.getByLabelText('Review outcome')).toHaveValue('non_responsive')
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/staff/record-items/${baseItem.id}/`,
        expect.anything(),
      )
    })

    resolveDetail(response(baseItem))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })

    expect(screen.getByLabelText('Review outcome')).toHaveValue('non_responsive')
  })
})
