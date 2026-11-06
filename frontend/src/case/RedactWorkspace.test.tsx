// ABOUTME: The Redact tab's workspace: responsive records beside a large page,
// ABOUTME: drag a box, pick the basis once, Enter adds, keys move and turn pages.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { RedactWorkspace } from './RedactWorkspace'

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

const RECORD = {
  id: 'rec-a',
  request_number: 'PRR-2026-0001',
  original_filename: 'Board memo.pdf',
  content_type: 'application/pdf',
  review_state: 'responsive',
  redaction_count: 0,
  second_review_required: false,
  second_reviewed_at: null,
  redacted_artifact_ready: false,
  handling: 'preview',
  handling_label: 'PDF',
  handling_detail: '',
  derivative_state: '',
  derivative_detail: '',
  reviewable: true,
}
const SECOND = { ...RECORD, id: 'rec-b', original_filename: 'Staff email.pdf', redaction_count: 2 }

const EXEMPTIONS = [
  { id: 9, citation: 'RCW 42.56.230(1)', short_name: 'Student information', requires_second_review: false, active: true },
  { id: 4, citation: 'RCW 42.56.250', short_name: 'Employee records', requires_second_review: false, active: true },
]

const PROPOSAL = {
  id: 'prop-1',
  advisory_run: 'run-1',
  record_item: 'rec-a',
  page_number: 1,
  x0: 0.1,
  y0: 0.2,
  x1: 0.4,
  y1: 0.3,
  exemption: 9,
  citation: 'RCW 42.56.230(1)',
  exemption_name: 'Student information',
  requires_second_review: false,
  confidence: 0.91,
  rationale: 'A student name.',
  source_span_ids: [],
  source_text_sha256: '',
  created_at: '2026-11-01T12:00:00Z',
  decision: null,
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

function setup({
  records = [RECORD, SECOND],
  proposals = [] as unknown[],
  redactions = [] as unknown[],
} = {}) {
  const posts: { url: string; body: unknown }[] = []
  const gets: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      posts.push({ url, body })
      if (url === '/api/staff/redactions/') {
        return response({ id: 50, ...body, citation: 'RCW 42.56.230(1)', requires_second_review: false }, 201)
      }
      return response({ ...PROPOSAL, decision: { outcome: 'accepted' } })
    }
    gets.push(url)
    if (url.startsWith('/api/staff/record-items/?')) return response(page(records))
    if (url.includes('/preview-metadata/')) return response({ page_count: 3 })
    if (url.startsWith('/api/staff/exemptions/')) return response(page(EXEMPTIONS))
    if (url.startsWith('/api/staff/redactions/')) return response(page(redactions))
    if (url.includes('/advisory-redactions/')) {
      return response({ enabled: true, latest: { state: 'complete' }, proposals })
    }
    if (url.includes('/redaction-batches/groups/')) {
      return response({ mode: 'suggest', sample_size: 10, groups: [], records_with_redactions: 0 })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/requests/PRR-2026-0001/redact']}>
          <RedactWorkspace number="PRR-2026-0001" />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
  return { posts, gets, user: userEvent.setup() }
}

async function drawBox(pageNumber = 1) {
  const surface = await screen.findByLabelText(`Draw redactions on page ${pageNumber}`)
  vi.spyOn(surface, 'getBoundingClientRect').mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 1000, width: 1000, height: 1000,
    toJSON: () => ({}),
  })
  fireEvent.pointerDown(surface, { clientX: 100, clientY: 200, pointerId: 1 })
  fireEvent.pointerMove(surface, { clientX: 800, clientY: 700, pointerId: 1 })
  fireEvent.pointerUp(surface, { clientX: 800, clientY: 700, pointerId: 1 })
}

it('opens the first responsive record and adds a drawn box with Enter', async () => {
  const { posts, gets, user } = setup()
  expect(await screen.findByRole('option', { name: /Board memo/ })).toHaveAttribute('aria-selected', 'true')
  expect(gets).toContain('/api/staff/record-items/?request=PRR-2026-0001&review_state=responsive')
  expect(screen.getByRole('option', { name: /Staff email.*2 redactions/ })).toBeInTheDocument()

  await drawBox()
  await user.selectOptions(await screen.findByLabelText('Legal basis'), '9')
  // The basis select keeps focus after choosing; Enter there still adds.
  await user.keyboard('{Enter}')

  await waitFor(() => expect(posts.at(-1)).toEqual({
    url: '/api/staff/redactions/',
    body: {
      record_item: 'rec-a', page_number: 1, x0: 0.1, y0: 0.2, x1: 0.8, y1: 0.7,
      exemption: 9, explanation: '',
    },
  }))
})

it('keeps the chosen basis when moving to the next record', async () => {
  const { user } = setup()
  await screen.findByRole('option', { name: /Board memo/ })
  await user.selectOptions(await screen.findByLabelText('Legal basis'), '4')
  ;(document.activeElement as HTMLElement | null)?.blur()
  await user.keyboard('j')
  expect(screen.getByRole('option', { name: /Staff email/ })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByLabelText('Legal basis')).toHaveValue('4')
})

it('turns pages with the arrow keys and zooms the page', async () => {
  const { user } = setup()
  await screen.findByLabelText('Draw redactions on page 1')
  expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()
  await user.keyboard('{ArrowRight}')
  expect(await screen.findByLabelText('Draw redactions on page 2')).toBeInTheDocument()
  await user.keyboard('{ArrowLeft}')
  expect(await screen.findByLabelText('Draw redactions on page 1')).toBeInTheDocument()

  expect(screen.getByText('100%')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Zoom in' }))
  expect(screen.getByText('150%')).toBeInTheDocument()
})

it('says where to go when nothing is responsive yet', async () => {
  setup({ records: [] })
  expect(await screen.findByText(/No responsive records yet/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Go to Review' })).toHaveAttribute(
    'href', '/requests/PRR-2026-0001/review',
  )
})

it('shows AI suggestions only when there are some, and accepts one', async () => {
  const { posts, user } = setup({ proposals: [PROPOSAL] })
  expect(await screen.findByText('A student name.')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Use suggestion' }))
  await user.click(screen.getByRole('button', { name: 'Accept suggestion' }))
  await waitFor(() => expect(posts.at(-1)).toEqual({
    url: '/api/staff/redaction-proposals/prop-1/accept/',
    body: { page_number: 1, x0: 0.1, y0: 0.2, x1: 0.4, y1: 0.3, exemption: 9, explanation: 'A student name.' },
  }))
})

it('hides the AI box when there are no suggestions', async () => {
  setup()
  await screen.findByLabelText('Draw redactions on page 1')
  expect(screen.queryByText(/AI suggestion/)).not.toBeInTheDocument()
})

it('removes a redaction only with a reason', async () => {
  const { posts, user } = setup({
    redactions: [{
      id: 7, record_item: 'rec-a', page_number: 1, x0: 0.1, y0: 0.1, x1: 0.2, y1: 0.2,
      exemption: 9, citation: 'RCW 42.56.230(1)', exemption_name: 'Student information',
      requires_second_review: false, explanation: '', created_by_username: 'hagelk',
      created_at: '2026-11-01T12:00:00Z', voided_at: null, void_reason: '',
    }],
  })
  await user.click(await screen.findByRole('button', { name: 'Remove' }))
  const confirm = screen.getByRole('button', { name: 'Confirm remove' })
  expect(confirm).toBeDisabled()
  await user.type(screen.getByLabelText(/Why remove it/), 'Drawn on the wrong line.')
  await user.click(confirm)
  await waitFor(() => expect(posts.at(-1)).toEqual({
    url: '/api/staff/redactions/7/void/', body: { reason: 'Drawn on the wrong line.' },
  }))
})

it('dismisses a suggestion only with a reason', async () => {
  const { posts, user } = setup({ proposals: [PROPOSAL] })
  await user.click(await screen.findByRole('button', { name: 'Use suggestion' }))
  const dismiss = screen.getByRole('button', { name: 'Dismiss suggestion' })
  expect(dismiss).toBeDisabled()
  await user.type(screen.getByLabelText(/Why dismiss it/), 'That is a staff name.')
  await user.click(dismiss)
  await waitFor(() => expect(posts.at(-1)).toEqual({
    url: '/api/staff/redaction-proposals/prop-1/reject/', body: { reason: 'That is a staff name.' },
  }))
})

it('approves a second review, then builds the redacted PDF', async () => {
  const saved = {
    id: 7, record_item: 'rec-a', page_number: 1, x0: 0.1, y0: 0.1, x1: 0.2, y1: 0.2,
    exemption: 9, citation: 'RCW 42.56.230(1)', exemption_name: 'Student information',
    requires_second_review: true, explanation: 'Names a student.', created_by_username: 'hagelk',
    created_at: '2026-11-01T12:00:00Z', voided_at: null, void_reason: '',
  }
  const { posts, user } = setup({
    records: [{ ...RECORD, redaction_count: 1, second_review_required: true }],
    redactions: [saved],
  })
  expect(await screen.findByText('Names a student.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Build redacted PDF' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Approve second review' }))
  await waitFor(() => expect(posts.at(-1)?.url).toBe('/api/staff/record-items/rec-a/second-review/'))
})

it('builds the redacted PDF when no second review is needed', async () => {
  const saved = {
    id: 7, record_item: 'rec-a', page_number: 2, x0: 0.1, y0: 0.1, x1: 0.2, y1: 0.2,
    exemption: 9, citation: 'RCW 42.56.230(1)', exemption_name: 'Student information',
    requires_second_review: false, explanation: '', created_by_username: 'hagelk',
    created_at: '2026-11-01T12:00:00Z', voided_at: null, void_reason: '',
  }
  const { posts, user } = setup({ redactions: [saved] })
  await user.click(await screen.findByRole('button', { name: /Page 2 · RCW/ }))
  expect(await screen.findByLabelText('Draw redactions on page 2')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Build redacted PDF' }))
  await waitFor(() => expect(posts.at(-1)?.url).toBe('/api/staff/record-items/rec-a/render-redactions/'))
})

it('moves between records and pages by click, and Esc clears a draft', async () => {
  const { user } = setup()
  await screen.findByLabelText('Draw redactions on page 1')
  await user.click(screen.getByRole('button', { name: 'Next page' }))
  expect(await screen.findByLabelText('Draw redactions on page 2')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Previous page' }))
  await drawBox()
  expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument()

  await user.click(screen.getByRole('option', { name: /Staff email/ }))
  expect(screen.getByRole('option', { name: /Staff email/ })).toHaveAttribute('aria-selected', 'true')
  await user.keyboard('k')
  expect(screen.getByRole('option', { name: /Board memo/ })).toHaveAttribute('aria-selected', 'true')
  await user.click(screen.getByRole('button', { name: 'Zoom in' }))
  await user.click(screen.getByRole('button', { name: 'Zoom out' }))
  expect(screen.getByText('100%')).toBeInTheDocument()
})
