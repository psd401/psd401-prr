// ABOUTME: Sampled batch review: every sampled record must be judged before
// ABOUTME: the batch can be decided, and the outcome is stated plainly.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { BatchReviewPanel, DiscardBatchCard } from './BatchReview'
import type { ReviewBatch } from './trustPolicy'

afterEach(() => vi.unstubAllGlobals())

const BATCH: ReviewBatch = {
  id: 7, kind: 'cluster', label: 'psd401.net', proposed_state: 'non_responsive', record_count: 12,
  samples: [
    { id: 's1', original_filename: 'one.eml', advisory_suggested_state: 'non_responsive', advisory_confidence: 0.9 },
    { id: 's2', original_filename: 'two.eml', advisory_suggested_state: null, advisory_confidence: null },
  ],
  outcome: 'open', applied_count: 0, skipped_count: 0,
}

function stub(responses: Record<string, unknown>) {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') posts.push({ url, body: JSON.parse(String(init.body)) })
    const key = Object.keys(responses).find((prefix) => url.startsWith(prefix))!
    return { ok: true, status: 200, text: async () => JSON.stringify(responses[key]) } as Response
  }))
  return posts
}

function wrap(node: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>)
}

it('decides only after every sampled record is judged, and says what happened', async () => {
  const posts = stub({
    '/api/staff/review-batches/7/decide/': { ...BATCH, outcome: 'applied', applied_count: 12 },
  })
  const onOpen = vi.fn()
  wrap(<BatchReviewPanel batch={BATCH} onOpen={onOpen} onClose={() => {}} />)

  expect(screen.getByText('Approve psd401.net as non-responsive')).toBeInTheDocument()
  const submit = screen.getByRole('button', { name: 'Record judgments' })
  await userEvent.selectOptions(screen.getByLabelText('Judgment for one.eml'), 'non_responsive')
  expect(submit).toBeDisabled()
  await userEvent.selectOptions(screen.getByLabelText('Judgment for two.eml'), 'non_responsive')
  await userEvent.click(screen.getByRole('button', { name: 'two.eml' }))
  expect(onOpen).toHaveBeenCalledWith('s2')
  await userEvent.click(submit)

  expect(await screen.findByText(/Applied: 12 records marked non-responsive in your name/)).toBeInTheDocument()
  expect(posts).toEqual([{
    url: '/api/staff/review-batches/7/decide/',
    body: { judgments: { s1: 'non_responsive', s2: 'non_responsive' } },
  }])
})

it('says a blocked batch leaves the rest for per-record review', async () => {
  stub({ '/api/staff/review-batches/7/decide/': { ...BATCH, outcome: 'blocked' } })
  wrap(<BatchReviewPanel batch={BATCH} onOpen={() => {}} onClose={() => {}} />)
  await userEvent.selectOptions(screen.getByLabelText('Judgment for one.eml'), 'responsive')
  await userEvent.selectOptions(screen.getByLabelText('Judgment for two.eml'), 'non_responsive')
  await userEvent.click(screen.getByRole('button', { name: 'Record judgments' }))
  expect(await screen.findByText(/Blocked: at least one sampled record/)).toBeInTheDocument()
})

it('offers the elusion check only in auto mode with records staged', async () => {
  const posts = stub({
    '/api/staff/review-batches/policy/': {
      responsive: 'suggest', non_responsive: 'auto', min_confidence: 0.95,
      batch_sample_size: 10, elusion_sample_size: 30, discard_candidates: 40,
    },
    '/api/staff/review-batches/discard/': { ...BATCH, kind: 'discard', record_count: 40 },
  })
  wrap(<DiscardBatchCard request="PRR-2026-0001" onOpen={() => {}} />)

  expect(await screen.findByText(/40 unreviewed records the AI calls non-responsive with at least 95%/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Start elusion check' }))
  expect(await screen.findByText('Elusion check: 40 records ready to discard')).toBeInTheDocument()
  await waitFor(() => expect(posts).toEqual([{
    url: '/api/staff/review-batches/discard/', body: { request: 'PRR-2026-0001' },
  }]))
})

it('shows nothing when non-responsive is not automatic', async () => {
  stub({
    '/api/staff/review-batches/policy/': {
      responsive: 'batch', non_responsive: 'batch', min_confidence: 0.95,
      batch_sample_size: 10, elusion_sample_size: 30, discard_candidates: 0,
    },
  })
  wrap(<DiscardBatchCard request="PRR-2026-0001" onOpen={() => {}} />)
  // Wait for the policy to load before asserting absence, so the card is
  // proven hidden by the policy and not merely by a pending query.
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    '/api/staff/review-batches/policy/?request=PRR-2026-0001', expect.anything(),
  ))
  await act(async () => {})
  expect(screen.queryByText('Ready to discard')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Start elusion check' })).not.toBeInTheDocument()
})
