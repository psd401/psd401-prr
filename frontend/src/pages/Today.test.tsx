// ABOUTME: Today lists every open case, most urgent first, each with its one
// ABOUTME: next step as a single link into the stage that does it.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { dueLabel, nextStep } from '../today'
import { Today } from './Today'

function response(payload: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(payload) }
}

function stage(id: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    label: id.charAt(0).toUpperCase() + id.slice(1),
    state: 'current',
    open_count: 1,
    summary: '',
    reason: '',
    ...extra,
  }
}

function row(number: string, extra: Record<string, unknown> = {}) {
  return {
    id: number,
    number,
    status: 'acknowledged',
    matter_kind: 'public_records',
    subject: `Subject of ${number}`,
    subject_source: 'ai',
    requester_name: 'A. Requester',
    requester_email: 'requester@example.org',
    next_due: null,
    next_due_basis: null,
    stage: stage('collect'),
    ...extra,
  }
}

const late = row('PRR-2026-0001', {
  status: 'received',
  next_due: '2026-10-30',
  next_due_basis: 'statutory',
  stage: stage('intake', { summary: 'Received and not yet acknowledged' }),
})
const reviewing = row('PRR-2026-0002', {
  next_due: '2026-11-10',
  next_due_basis: 'estimate',
  stage: stage('review', { open_count: 2324, summary: '2324 records to review' }),
})

function page(results: unknown[]) {
  return { count: results.length, next: null, previous: null, results }
}

function stubApi(cases: unknown[], batches: unknown[] = [], counts: Record<string, number> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/decision-batches/')) return response(page(batches))
    if (url.includes('/work-items/counts/')) return response(counts)
    return response(page(cases))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={new QueryClient()}>
        <Today />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-11-06T16:00:00Z'))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('lists open cases by due date, each with one next step', async () => {
  const fetchMock = stubApi([late, reviewing])
  renderPage()

  const items = await screen.findAllByRole('listitem')
  expect(items).toHaveLength(2)
  expect(within(items[0]!).getByText('Subject of PRR-2026-0001')).toBeInTheDocument()
  expect(within(items[0]!).getByText('7 days late')).toBeInTheDocument()
  expect(within(items[0]!).getByRole('link', { name: 'Send acknowledgment' }))
    .toHaveAttribute('href', '/requests/PRR-2026-0001/messages')
  expect(within(items[1]!).getByText('Due in 4 days')).toBeInTheDocument()
  expect(within(items[1]!).getByRole('link', { name: 'Review 2,324 records' }))
    .toHaveAttribute('href', '/requests/PRR-2026-0002/review')
  expect(screen.getByText('2 open cases · 1 late')).toBeInTheDocument()
  expect(fetchMock.mock.calls.map(([url]) => String(url)).some((url) => url.includes('status=open&ordering=due'))).toBe(true)
})

it('searches every case, open or closed, from one box', async () => {
  const fetchMock = stubApi([late])
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
  renderPage()
  await screen.findAllByRole('listitem')

  await user.type(screen.getByRole('searchbox', { name: 'Search cases' }), 'levy{Enter}')

  await screen.findByText('Results for “levy”')
  const searched = fetchMock.mock.calls.map(([url]) => String(url)).find((url) => url.includes('search='))
  expect(searched).toContain('search=levy')
  expect(searched).not.toContain('status=open')
})

it('points to work that is not on a case yet', async () => {
  stubApi([late], [{ id: 'b1' }, { id: 'b2' }], { inbox_unmatched: 1, inbox_quarantined: 0, inbox_delivery_exceptions: 3 })
  renderPage()

  expect(await screen.findByRole('link', { name: '2 decisions waiting' }))
    .toHaveAttribute('href', '/decisions')
  expect(screen.getByRole('link', { name: '1 email needs a case' }))
    .toHaveAttribute('href', '/inbox?view=unmatched')
  expect(screen.getByRole('link', { name: '3 messages did not deliver' }))
    .toHaveAttribute('href', '/inbox?view=delivery_exceptions')
  expect(screen.queryByText(/held for review/)).not.toBeInTheDocument()
})

it('says so when nothing is open', async () => {
  stubApi([])
  renderPage()

  expect(await screen.findByText('Nothing open. Enjoy the quiet.')).toBeInTheDocument()
})

it('names the next step for every stage', () => {
  const at = (id: string, extra: Record<string, unknown> = {}) =>
    nextStep({ ...row('PRR-2026-0009'), stage: stage(id, extra) } as never)
  expect(at('intake').label).toBe('Send acknowledgment')
  expect(nextStep({ ...row('PRR-2026-0009'), matter_kind: 'litigation_discovery', stage: stage('intake') } as never))
    .toEqual({ label: 'Triage', path: '/requests/PRR-2026-0009/intake', waiting: false })
  expect(at('scope').label).toBe('Set the scope')
  expect(at('scope', { state: 'blocked', reason: 'Waiting on the requester’s clarification' }))
    .toEqual({ label: 'Waiting on requester', path: '/requests/PRR-2026-0009/scope', waiting: true })
  expect(at('collect').label).toBe('Finish collection')
  expect(at('review', { open_count: 1 }).label).toBe('Review 1 record')
  expect(at('redact', { open_count: 3 }).label).toBe('Decide 3 redactions')
  expect(at('release').label).toBe('Release records')
  expect(at('release', { state: 'blocked' }).label).toBe('Waiting on deposit')
  expect(at('close').label).toBe('Close the case')
  expect(at('close', { state: 'done' }).label).toBe('View case')
})

it('words a due date the way a person says it', () => {
  expect(dueLabel('2026-11-05')).toEqual({ text: '1 day late', tone: 'late' })
  expect(dueLabel('2026-11-06')).toEqual({ text: 'Due today', tone: 'soon' })
  expect(dueLabel('2026-11-07')).toEqual({ text: 'Due tomorrow', tone: 'soon' })
  expect(dueLabel('2026-11-13')).toEqual({ text: 'Due in 7 days', tone: 'calm' })
  expect(dueLabel('2026-12-24')).toEqual({ text: 'Due Dec 24', tone: 'calm' })
  expect(dueLabel('2027-01-06')).toEqual({ text: 'Due Jan 6, 2027', tone: 'calm' })
  expect(dueLabel(null)).toEqual({ text: 'No due date', tone: 'none' })
})
