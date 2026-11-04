// ABOUTME: The suggested intake label is confirmed or corrected by a person,
// ABOUTME: and confirming only opens the ordinary action through a callback.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { IntakeClassificationCard } from './IntakeClassificationCard'

function response(body: unknown, status = 200): Response {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

const COMPLETE = {
  enabled: true,
  latest: {
    id: 'run-1',
    state: 'complete',
    model_identifier: 'us.anthropic.claude-opus-5-5',
    proposal: {
      label: 'duplicate_of',
      target_case_number: 'PRR-2026-0001',
      confidence: 0.92,
      evidence: 'Same wording as the earlier request.',
    },
    failure_detail: '',
  },
  feedback: null,
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function setup(latest: unknown = COMPLETE) {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null })
      return response({ outcome: 'confirmed' }, 201)
    }
    return response(latest)
  }))
  const onConfirm = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <IntakeClassificationCard
          number="PRR-2026-0002"
          emailAutoCreated={false}
          onConfirm={onConfirm}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { posts, onConfirm, user: userEvent.setup() }
}

it('shows the label, its target and evidence, and confirms through the callback', async () => {
  const { posts, onConfirm, user } = setup()

  expect(await screen.findByText('Duplicate of')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'PRR-2026-0001' })).toBeInTheDocument()
  expect(screen.getByText('92% confidence')).toBeInTheDocument()
  expect(screen.getByText('Same wording as the earlier request.')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Correct' }))

  await waitFor(() => expect(onConfirm).toHaveBeenCalledWith('duplicate_of', 'PRR-2026-0001'))
  expect(posts).toEqual([{
    url: '/api/staff/requests/PRR-2026-0002/advisory-intake/feedback/',
    body: { outcome: 'confirmed', final_label: 'duplicate_of', reason: '' },
  }])
})

it('records a correction without opening any action', async () => {
  const { posts, onConfirm, user } = setup()

  await user.click(await screen.findByRole('button', { name: 'Not correct' }))
  await user.selectOptions(screen.getByLabelText('What is it?'), 'new_request')
  await user.type(screen.getByLabelText('Why (optional)'), 'Different school year.')
  await user.click(screen.getByRole('button', { name: 'Save correction' }))

  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0]?.body).toEqual({
    outcome: 'dismissed', final_label: 'new_request', reason: 'Different school year.',
  })
  expect(onConfirm).not.toHaveBeenCalled()
})

it('renders nothing while advisory AI is off', async () => {
  setup({ enabled: false, latest: null, feedback: null })
  await waitFor(() => expect(screen.queryByText('Suggested intake label')).not.toBeInTheDocument())
})

it('reopens the action after a confirmed label, so a cancelled dialog is not a dead end', async () => {
  const { posts, onConfirm, user } = setup({
    ...COMPLETE,
    feedback: { outcome: 'confirmed', final_label: 'duplicate_of' },
  })

  expect(await screen.findByText(/You agreed with this label/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Open the action' }))

  expect(onConfirm).toHaveBeenCalledWith('duplicate_of', 'PRR-2026-0001')
  expect(posts).toEqual([])
})

it('offers no action after agreeing to a label that has none', async () => {
  setup({
    ...COMPLETE,
    latest: {
      ...COMPLETE.latest,
      proposal: { label: 'spam', target_case_number: null, confidence: 0.9, evidence: 'Ad.' },
    },
    feedback: { outcome: 'confirmed', final_label: 'spam' },
  })

  expect(await screen.findByText(/You agreed with this label/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Open the action' })).not.toBeInTheDocument()
})
