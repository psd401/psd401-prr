// ABOUTME: A records list must state its true size and file types, because a
// ABOUTME: list that looks complete is how an inadequate search gets certified.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { RecordItems } from './RecordItems'
import { at } from '../test/at'

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function item(index: number, overrides: Record<string, unknown> = {}) {
  return {
    id: `${index}`.padStart(8, '0') + '-1111-4111-8111-111111111111',
    request_number: '26-19',
    source_system: 'nextrequest_legacy',
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
    custodian: '',
    original_filename: `2018 Resources/policy-${index}.pdf`,
    sha256: `${index}`.padStart(64, 'a'),
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
    content_type: 'application/pdf',
    content_length: 12345,
    handling: 'preview',
    handling_label: 'Ready to review',
    handling_detail: 'Opens in the reviewer and supports redaction.',
    ...overrides,
  }
}

/** One page of a 285-item result set, exactly as DRF returns it. */
function serverPage(pageNumber: number, total = 285, size = 50) {
  const start = (pageNumber - 1) * size
  const results = Array.from(
    { length: Math.min(size, total - start) },
    (_, offset) => item(start + offset),
  )
  return {
    count: total,
    next: start + size < total ? `?page=${pageNumber + 1}` : null,
    previous: pageNumber > 1 ? `?page=${pageNumber - 1}` : null,
    results,
  }
}

function renderList() {
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

describe('records list at volume', () => {
  it('states the full total when only one page has been fetched', async () => {
    // The defect this replaces: 50 rows rendered, 235 invisible, nothing on
    // screen indicating the list was truncated.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/record-items/')) {
        return response(serverPage(1))
      }
      return response({}, 404)
    }))
    renderList()
    expect(await screen.findByText(/Showing 1–50 of 285 files/)).toBeTruthy()
    expect(screen.getByText(/Page 1 of 6/)).toBeTruthy()
  })

  it('fetches the next page instead of silently ending at the first', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/record-items/')) {
        const match = /[?&]page=(\d+)/.exec(url)
        return response(serverPage(match ? Number(match[1]) : 1))
      }
      return response({}, 404)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderList()
    await screen.findByText(/Showing 1–50 of 285 files/)

    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByText(/Showing 51–100 of 285 files/)).toBeTruthy()
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) => String(input).includes('page=2')),
      ).toBe(true)
    })
  })

  it('does not offer paging when everything already fits on one page', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/record-items/')) {
        return response(serverPage(1, 12))
      }
      return response({}, 404)
    }))
    renderList()
    // The total still appears: "12 files" should be something the reviewer
    // read, not something they inferred from counting rows.
    expect(await screen.findByText(/Showing 1–12 of 12 files/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull()
  })

  it('marks files no reviewer can open, rather than failing on a click', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/record-items/')) {
        return response({
          count: 3,
          next: null,
          previous: null,
          results: [
            item(1),
            item(2, {
              original_filename: 'Prezi/portable.swf',
              content_type: 'application/x-shockwave-flash',
              handling: 'opaque',
              handling_label: 'No renderer available',
            }),
            item(3, {
              original_filename: 'bus-camera.mp4',
              content_type: 'video/mp4',
              handling: 'media',
              handling_label: 'Audio or video',
            }),
          ],
        })
      }
      return response({}, 404)
    }))
    renderList()
    // Scoped to the table: the same words are also filter chips above it.
    await screen.findByText('Prezi/portable.swf')
    const table = screen.getByRole('table')
    const badges = within(table).getAllByText(
      /^(Ready to review|Needs conversion|Audio or video|No renderer available)$/,
    )
    expect(badges.map((node) => node.textContent)).toEqual([
      'Ready to review',
      'No renderer available',
      'Audio or video',
    ])
  })

  it('asks the server to search records rather than filtering one page', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/record-items/')) {
        return response(serverPage(1))
      }
      return response({}, 404)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderList()
    await screen.findByText(/Showing 1–50 of 285 files/)

    await user.type(screen.getByLabelText(/^Search records/), '2018 Resources')
    await user.click(screen.getByRole('button', { name: 'Search' }))

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes('search=2018+Resources'),
        ),
      ).toBe(true)
    })
  })

  it('returns to the first page when the filter changes', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/record-items/')) {
        const match = /[?&]page=(\d+)/.exec(url)
        return response(serverPage(match ? Number(match[1]) : 1))
      }
      return response({}, 404)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderList()
    await screen.findByText(/Showing 1–50 of 285 files/)
    await user.click(screen.getByRole('button', { name: 'Next' }))
    await screen.findByText(/Showing 51–100 of 285 files/)

    // Staying on page 2 of a freshly filtered list shows an empty table for no
    // reason the reviewer can see.
    // File type is a compact select now: review state is the axis reviewers
    // work along, so it keeps the chips, and this narrower cut does not.
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'File type' }),
      'convert',
    )

    await waitFor(() => {
      const last = String(
        at(fetchMock.mock.calls, fetchMock.mock.calls.length - 1)[0],
      )
      expect(last).toContain('handling=convert')
      expect(last).not.toContain('page=2')
    })
  })
})
