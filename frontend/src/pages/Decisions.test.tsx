// ABOUTME: The decision queue shows each proposed batch with its evidence one
// ABOUTME: click away, approves in one click, and rejects only with a reason.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Decisions } from './Decisions'

function response(payload: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(payload) }
}

const batch = {
  id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  kind: 'review',
  request_number: 'PRR-2026-0042',
  payload: { record_item_ids: ['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'], state: 'non_responsive' },
  evidence_summary: 'The newsletter, not board minutes.',
  proposed_by_username: 'agent-owner',
  created_at: '2026-11-04T19:00:00Z',
  state: 'proposed',
  decided_by_username: null,
  decided_at: null,
  decision_reason: '',
  result: {},
  records: [{ id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', original_filename: 'newsletter.eml', review_state: 'unreviewed' }],
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={new QueryClient()}>
        <Decisions />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

it('shows what approving does, with the evidence one click away', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ count: 1, next: null, previous: null, results: [batch] })))
  const user = userEvent.setup()
  renderPage()

  expect(await screen.findByText('Mark 1 record non-responsive.')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'PRR-2026-0042' })).toHaveAttribute('href', '/requests/PRR-2026-0042/records')
  await user.click(screen.getByText('Evidence'))
  expect(screen.getByText('The newsletter, not board minutes.')).toBeVisible()
  expect(screen.getByText('newsletter.eml — now unreviewed')).toBeVisible()
})

it('approves in one click', async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, options?: RequestInit) => (
    options?.method === 'POST'
      ? response({ ...batch, state: 'approved' })
      : response({ count: 1, next: null, previous: null, results: [batch] })
  ))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  await user.click(await screen.findByRole('button', { name: 'Approve' }))
  expect(fetchMock).toHaveBeenCalledWith(
    `/api/staff/decision-batches/${batch.id}/approve/`,
    expect.objectContaining({ method: 'POST' }),
  )
})

it('rejects only with a reason', async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, options?: RequestInit) => (
    options?.method === 'POST'
      ? response({ ...batch, state: 'rejected' })
      : response({ count: 1, next: null, previous: null, results: [batch] })
  ))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  await user.click(await screen.findByRole('button', { name: 'Reject' }))
  const confirm = screen.getByRole('button', { name: 'Reject batch' })
  expect(confirm).toBeDisabled()
  await user.type(screen.getByLabelText(/Why reject it/), 'These are the minutes.')
  await user.click(confirm)
  expect(fetchMock).toHaveBeenCalledWith(
    `/api/staff/decision-batches/${batch.id}/reject/`,
    expect.objectContaining({ method: 'POST', body: JSON.stringify({ reason: 'These are the minutes.' }) }),
  )
})

it('shows a refused approval and an empty queue plainly', async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, options?: RequestInit) => (
    options?.method === 'POST'
      ? response({ detail: 'This batch was already decided.' }, 400)
      : response({ count: 1, next: null, previous: null, results: [batch] })
  ))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  const { unmount } = renderPage()
  await user.click(await screen.findByRole('button', { name: 'Approve' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('This batch was already decided.')
  unmount()

  vi.stubGlobal('fetch', vi.fn(async () => response({ count: 0, next: null, previous: null, results: [] })))
  renderPage()
  const empty = await screen.findByText('Nothing is waiting for a decision.')
  expect(within(empty.parentElement as HTMLElement).queryByRole('button')).toBeNull()
})
