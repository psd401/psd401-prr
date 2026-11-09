// ABOUTME: Redaction proposal groups: sampled group acceptance needs a decision
// ABOUTME: on every sampled proposal (a reason to reject), and builds PDFs.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { RedactionProposals } from './RedactionProposals'

afterEach(() => vi.unstubAllGlobals())

const GROUPS = {
  mode: 'batch', sample_size: 2, records_with_redactions: 2,
  groups: [{
    exemption_id: 4, exemption_citation: 'RCW 42.56.250(4)', exemption_name: 'Employee phone',
    requires_second_review: false, pattern: 'phone', pattern_label: 'Phone number', pending: 5, records: 3,
  }],
}
const BATCH = {
  id: 9, exemption_citation: 'RCW 42.56.250(4)', pattern_label: 'Phone number', proposal_count: 5,
  samples: [
    { id: 'p1', record_item_id: 'r1', original_filename: 'a.pdf', page_number: 1, confidence: 0.9, rationale: 'A cell number.' },
    { id: 'p2', record_item_id: 'r2', original_filename: 'b.pdf', page_number: 2, confidence: 0.8, rationale: 'A home number.' },
  ],
  outcome: 'open', accepted_count: 0, skipped_count: 0,
}

function stub(decideResult: unknown) {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    let body: unknown = GROUPS
    if (init?.method === 'POST') {
      posts.push({ url, body: JSON.parse(String(init.body)) })
      body = url.endsWith('/decide/') ? decideResult
        : url.endsWith('/build/') ? { built: 2, awaiting_second_review: 1, already_current: 0, failed: 1 }
        : BATCH
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response
  }))
  return posts
}

function setup() {
  const onOpen = vi.fn()
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RedactionProposals request="PRR-2026-0001" onOpen={onOpen} />
    </QueryClientProvider>,
  )
  return onOpen
}

it('accepts a group only after every sampled proposal is decided, with a reason to reject', async () => {
  const posts = stub({ ...BATCH, outcome: 'blocked' })
  const onOpen = setup()

  expect(await screen.findByText('Phone number')).toBeInTheDocument()
  expect(screen.getByText(/RCW 42.56.250\(4\) · 5 proposals on 3 records/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Accept all in group' }))

  const submit = await screen.findByRole('button', { name: 'Record decisions' })
  await userEvent.selectOptions(screen.getByLabelText('Decision for a.pdf'), 'accept')
  await userEvent.selectOptions(screen.getByLabelText('Decision for b.pdf'), 'reject')
  expect(submit).toBeDisabled()
  await userEvent.type(screen.getByLabelText(/Why reject/), 'District main office line.')
  await userEvent.click(screen.getByRole('button', { name: 'b.pdf, page 2' }))
  expect(onOpen).toHaveBeenCalledWith('r2')
  await userEvent.click(submit)

  expect(await screen.findByText(/Blocked: you rejected a sampled proposal/)).toBeInTheDocument()
  expect(posts.at(-1)).toEqual({
    url: '/api/staff/redaction-batches/9/decide/',
    body: { judgments: {
      p1: { accept: true, reason: '' },
      p2: { accept: false, reason: 'District main office line.' },
    } },
  })
})

it('builds every ready redacted PDF in one action', async () => {
  const posts = stub(BATCH)
  setup()
  await userEvent.click(await screen.findByRole('button', { name: 'Build redacted PDFs' }))
  expect(await screen.findByText(/Built 2. 1 await second review./)).toBeInTheDocument()
  expect(screen.getByText(/1 could not be built/)).toBeInTheDocument()
  await waitFor(() => expect(posts).toEqual([{
    url: '/api/staff/redaction-batches/build/', body: { request: 'PRR-2026-0001' },
  }]))
})
