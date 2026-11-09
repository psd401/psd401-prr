// ABOUTME: The AI evaluation screen shows each model's metrics side by side
// ABOUTME: and says plainly when a cost cannot be computed.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { AiEvaluation } from './AiEvaluation'

afterEach(() => vi.unstubAllGlobals())

const RESULT = {
  items: 4, errors: 0, accuracy: 0.75, safety_recall: 1, target_accuracy: 0.5,
  confusion: { new: { new: 2 }, not_request: { new: 1, not_request: 0 } },
  input_tokens_mean: 1200, output_tokens_mean: 90, cost_per_item_usd: 0.0049,
  latency_p50_ms: 800, latency_p95_ms: 1500,
}

it('lists every model with accuracy, safety recall, and cost', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    text: async () => JSON.stringify([{
      id: 'run-1', kind: 'intake', items: 4, label_counts: { new: 2, not_request: 2 },
      model_identifiers: ['model-a', 'model-b'],
      results: { 'model-a': RESULT, 'model-b': { ...RESULT, accuracy: 0.5, cost_per_item_usd: null } },
      created_by: 'officer@psd401.net', created_at: '2026-11-04T08:00:00Z',
    }]),
  }) as Response))
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><AiEvaluation /></MemoryRouter>
    </QueryClientProvider>,
  )
  const table = await screen.findByRole('table', { name: 'Intake classification model comparison' })
  const a = within(table).getByText('model-a').closest('tr')!
  expect(within(a).getByText('75%')).toBeInTheDocument()
  expect(within(a).getByText('100%')).toBeInTheDocument()
  expect(within(a).getByText('$0.0049')).toBeInTheDocument()
  expect(within(a).getByText('not_request → new: 1')).toBeInTheDocument()
  const b = within(table).getByText('model-b').closest('tr')!
  expect(within(b).getByText('no price')).toBeInTheDocument()
})
