// ABOUTME: The scope trail lists each count with the agent's reason and the
// ABOUTME: final proposed scope, and a re-run needs a stated reason.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { ScopingTrail } from './ScopingTrail'

afterEach(() => vi.unstubAllGlobals())

const iteration = (n: number, total: number | null, reason: string, scope = 'entire_org') => ({
  number: n, reason, query: `"route 12" AND -from:news@psd401.net #${n}`, search_scope: scope,
  custodians: scope === 'accounts' ? ['transportation@psd401.net'] : [], state: total === null ? 'failed' : 'complete',
  total, top_accounts: [{ email: 'transportation@psd401.net', count: total ?? 0 }],
  date_range_start: '2026-01-01', date_range_end: '2026-03-01',
})

function setup(session: unknown, postStatus = 202) {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts.push({ url: String(input), body: JSON.parse(String(init.body)) })
      return { ok: postStatus < 400, status: postStatus, text: async () => JSON.stringify(postStatus < 400 ? { session } : ['Google Vault counting is not configured.']) } as Response
    }
    return { ok: true, status: 200, text: async () => JSON.stringify({ session }) } as Response
  }))
  render(<QueryClientProvider client={new QueryClient()}><ScopingTrail number="PRR-2026-0001" /></QueryClientProvider>)
  return { posts, user: userEvent.setup() }
}

it('shows each count, why it changed, and the proposed scope', async () => {
  const first = iteration(1, 900, 'Transportation holds route mail.')
  const second = iteration(2, 120, 'Most hits were the district newsletter.', 'accounts')
  setup({
    id: 1, trigger: 'automatic', actor: null, reason: '', state: 'complete',
    stop_reason: 'The scope is now specific.', max_iterations: 4,
    iterations: [first, second], final: second,
  })
  expect(await screen.findByText('Most hits were the district newsletter.')).toBeInTheDocument()
  expect(screen.getByText('900 messages')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Proposed scope: count 2, 120 messages in 1 named mailbox. The scope is now specific.')
  expect(screen.getAllByText(/Most matches: transportation@psd401.net/)).toHaveLength(2)
})

it('re-runs only with a reason, and shows a refusal', async () => {
  const { posts, user } = setup(null, 400)
  expect(await screen.findByText(/No scoping has run/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Re-run scoping' }))
  const run = screen.getByRole('button', { name: 'Run scoping' })
  expect(run).toBeDisabled()
  await user.type(screen.getByLabelText(/Why run scoping again/), 'The custodian list changed.')
  await user.click(run)
  await waitFor(() => expect(posts).toEqual([{
    url: '/api/staff/requests/PRR-2026-0001/scoping/', body: { reason: 'The custodian list changed.' },
  }]))
  expect(await screen.findByText(/Google Vault counting is not configured/)).toBeInTheDocument()
})

it('says when scoping stopped on a failed count', async () => {
  setup({
    id: 2, trigger: 'manual', actor: 'officer@psd401.net', reason: 'New custodian.', state: 'failed',
    stop_reason: 'The count failed (provider_timeout).', max_iterations: 4,
    iterations: [iteration(1, null, 'Initial.')], final: null,
  })
  expect(await screen.findByText(/Re-run by officer@psd401.net: New custodian./)).toBeInTheDocument()
  expect(screen.getByText('count failed')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Scoping stopped: The count failed (provider_timeout).')
})

function setupWith(body: unknown) {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts.push({ url: String(input), body: JSON.parse(String(init.body)) })
      return { ok: true, status: 202, text: async () => '{}' } as Response
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response
  }))
  render(<QueryClientProvider client={new QueryClient()}><ScopingTrail number="PRR-2026-0001" /></QueryClientProvider>)
  return { posts, user: userEvent.setup() }
}

const FINAL = { ...iteration(2, 120, 'Tightened.', 'accounts'), count_run_id: 'run-2', input_sha256: 'h2' }
const DONE = {
  id: 1, trigger: 'automatic', actor: null, reason: '', state: 'complete', stop_reason: 'Specific.',
  max_iterations: 4, iterations: [FINAL], final: FINAL,
}

it('approves the proposed scope and collects it in one action', async () => {
  const { posts, user } = setupWith({ session: DONE, current_collection: null })
  await user.click(await screen.findByRole('button', { name: 'Approve and collect' }))
  expect(screen.getByText(/with a hold on the named mailboxes/)).toBeInTheDocument()
  await waitFor(() => expect(posts).toEqual([{
    url: '/api/staff/requests/PRR-2026-0001/vault-collection/',
    body: { count_run_id: 'run-2', expected_input_sha256: 'h2', confirmed: true },
  }]))
})

it('says when the trust policy already started the collection', async () => {
  setupWith({
    session: DONE,
    current_collection: { task_id: 7, state: 'running', trigger: 'automatic', count_run_id: 'run-2', total: 120 },
  })
  expect(await screen.findByText(/Collecting this scope \(running\), started automatically by the trust policy/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Approve and collect' })).not.toBeInTheDocument()
})

it('shows the re-scope diff and needs a reason to approve it', async () => {
  const { posts, user } = setupWith({
    session: DONE,
    current_collection: { task_id: 7, state: 'complete', trigger: 'manual', count_run_id: 'run-1', total: 2340 },
  })
  expect(await screen.findByText(/current collection has 2,340 messages; the proposed scope has 120 messages/)).toBeInTheDocument()
  const approve = screen.getByRole('button', { name: 'Approve re-scope' })
  expect(approve).toBeDisabled()
  await user.type(screen.getByLabelText(/Why change the scope/), 'The newsletter made up most of the old collection.')
  await user.click(approve)
  await waitFor(() => expect(posts).toEqual([{
    url: '/api/staff/requests/PRR-2026-0001/rescope/',
    body: { count_run_id: 'run-2', expected_input_sha256: 'h2', reason: 'The newsletter made up most of the old collection.' },
  }]))
})
