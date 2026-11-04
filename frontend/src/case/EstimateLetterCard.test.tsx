// ABOUTME: The estimate-letter card previews a system-prepared draft and sends
// ABOUTME: it through the ordinary approve-and-send primitive in one click.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui/Toast'
import { EstimateLetterCard } from './EstimateLetterCard'

afterEach(() => vi.unstubAllGlobals())

const DRAFT = {
  correspondence: {
    id: 'letter-1', version: 3, status: 'draft', system_prepared: true,
    subject: 'Public Records Request PRR-2026-0001 — How We Understand Your Request',
    body: 'How the District understands your request:\n\nRoute 12 bus video.\n',
  },
  estimate: { id: 'est-1', as_of: '2026-11-09' },
}

function setup(letter: unknown) {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      posts.push({ url, body: JSON.parse(String(init.body)) })
      return { ok: true, status: 200, text: async () => '{}' } as Response
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(letter) } as Response
  }))
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ToastProvider><EstimateLetterCard number="PRR-2026-0001" /></ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return posts
}

it('previews the draft and approves and sends it in one action', async () => {
  const posts = setup(DRAFT)

  expect(await screen.findByText('Interpretation and estimate letter ready')).toBeInTheDocument()
  expect(screen.getByText(/Drafted by the system from the estimate computed 2026-11-09/)).toBeInTheDocument()
  expect(screen.getByLabelText('Letter preview')).toHaveTextContent('Route 12 bus video.')
  expect(screen.getByRole('link', { name: 'Edit in Messages' }))
    .toHaveAttribute('href', '/requests/PRR-2026-0001/messages')

  await userEvent.click(screen.getByRole('button', { name: 'Approve and send' }))

  await waitFor(() => expect(posts).toEqual([{
    url: '/api/staff/correspondence/letter-1/approve-and-send/',
    body: { expected_version: 3 },
  }]))
})

it('renders nothing when no letter is waiting', async () => {
  setup({ correspondence: null, estimate: null })
  await waitFor(() => expect(screen.queryByText('Interpretation and estimate letter ready')).not.toBeInTheDocument())
})
