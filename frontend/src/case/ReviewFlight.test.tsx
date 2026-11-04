// ABOUTME: Keyboard review: one key decides and advances, u undoes, x selects
// ABOUTME: for a bulk decision, and Cmd-K runs any command from a palette.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { ReviewFlight } from './ReviewFlight'

afterEach(() => vi.unstubAllGlobals())

const ROWS = [
  { id: 'a', original_filename: 'a.pdf', review_state: 'unreviewed', reviewable: true, advisory_suggested_state: 'responsive', advisory_confidence: 0.92 },
  { id: 'b', original_filename: 'b.pdf', review_state: 'unreviewed', reviewable: true, advisory_suggested_state: 'non_responsive', advisory_confidence: 0.81 },
  { id: 'c', original_filename: 'c.pdf', review_state: 'unreviewed', reviewable: false, advisory_suggested_state: null, advisory_confidence: null },
]

function setup() {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    let body: unknown = { count: 3, next: null, previous: null, results: ROWS }
    if (url.includes('/advisory-responsiveness/')) {
      body = { latest: { state: 'complete', proposal: { suggested_state: 'responsive', confidence: 0.92, rationale: 'Mentions the bus route.' } } }
    }
    if (init?.method === 'POST') {
      posts.push({ url, body: JSON.parse(String(init.body)) })
      body = url.includes('undo-review') ? { ...ROWS[0], review_state: 'unreviewed' } : { ...ROWS[0], review_state: 'responsive' }
      if (url.endsWith('/bulk-review/')) body = []
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response
  }))
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><ReviewFlight number="PRR-2026-0001" /></MemoryRouter>
    </QueryClientProvider>,
  )
  return { posts, user: userEvent.setup() }
}

it('shows the AI label inline and decides, undoes, and moves by key', async () => {
  const { posts, user } = setup()
  expect(await screen.findByText('Mentions the bus route.')).toBeInTheDocument()
  expect(screen.getByText(/AI Responsive 92%/)).toBeInTheDocument()

  await user.keyboard('j')
  expect(screen.getByRole('option', { name: /b\.pdf/ })).toHaveAttribute('aria-selected', 'true')
  await user.keyboard('k')
  await user.keyboard('r')
  await waitFor(() => expect(posts.at(-1)).toEqual({
    url: '/api/staff/record-items/a/review/', body: { state: 'responsive' },
  }))
  expect(await screen.findByText(/a\.pdf: Responsive\./)).toBeInTheDocument()

  await user.keyboard('u')
  await waitFor(() => expect(posts.at(-1)?.url).toBe('/api/staff/record-items/a/undo-review/'))
  expect(await screen.findByText(/Undone: a\.pdf is Unreviewed\./)).toBeInTheDocument()
})

it('decides a selection together and refuses a bulk duplicate', async () => {
  const { posts, user } = setup()
  await screen.findByText('Mentions the bus route.')
  await user.keyboard('x')
  await user.keyboard('j')
  await user.keyboard('x')
  expect(screen.getByText(/2 selected/)).toBeInTheDocument()
  await user.keyboard('d')
  expect(screen.getByText(/A duplicate is decided one record at a time/)).toBeInTheDocument()
  await user.keyboard('n')
  await waitFor(() => expect(posts.at(-1)).toEqual({
    url: '/api/staff/record-items/bulk-review/',
    body: { record_item_ids: ['a', 'b'], state: 'non_responsive' },
  }))
})

it('runs a command from the palette', async () => {
  const { posts, user } = setup()
  await screen.findByText('Mentions the bus route.')
  await user.keyboard('{Meta>}k{/Meta}')
  const search = await screen.findByLabelText('Search commands')
  await user.type(search, 'non-resp{Enter}')
  await waitFor(() => expect(posts.at(-1)).toEqual({
    url: '/api/staff/record-items/a/review/', body: { state: 'non_responsive' },
  }))
})
