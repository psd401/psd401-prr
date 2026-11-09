// ABOUTME: The AI agreement screen reports agreement by label and confidence
// ABOUTME: band, district-wide by default and for one case on request.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { AiAgreement } from './AiAgreement'

afterEach(() => vi.unstubAllGlobals())

const REPORT = {
  decisions: 5, scored: 4, agreed: 3, agreement: 0.75, uncertain: 1, without_suggestion: 2,
  rows: [
    { label: 'non_responsive', band: '90-100%', decisions: 3, agreed: 2, agreement: 0.6667 },
    { label: 'responsive', band: '50-69%', decisions: 1, agreed: 1, agreement: 1 },
    { label: 'uncertain', band: 'below 50%', decisions: 1, agreed: null, agreement: null },
  ],
}

it('shows agreement by label and band, then narrows to one case', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    urls.push(url)
    const request = new URL(url, 'http://test').searchParams.get('request')
    return { ok: true, status: 200, text: async () => JSON.stringify({ request, ...REPORT }) } as Response
  }))
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><AiAgreement /></MemoryRouter>
    </QueryClientProvider>,
  )

  expect(await screen.findByText('District-wide')).toBeInTheDocument()
  expect(screen.getByText('75%')).toBeInTheDocument()
  const table = screen.getByRole('table', { name: 'Agreement by suggested label and confidence' })
  const row = within(table).getByText('Non-responsive').closest('tr')!
  expect(within(row).getByText('66.7%')).toBeInTheDocument()
  const uncertain = within(table).getByText('Uncertain').closest('tr')!
  expect(within(uncertain).getAllByText('—')).toHaveLength(2)

  await userEvent.type(screen.getByLabelText(/Case number/), 'PRR-2026-0001')
  await userEvent.click(screen.getByRole('button', { name: 'Show' }))
  expect(await screen.findByText('Case PRR-2026-0001')).toBeInTheDocument()
  expect(urls.at(-1)).toBe('/api/staff/advisory-agreement/?request=PRR-2026-0001')
})
