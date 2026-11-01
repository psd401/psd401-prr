import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueueCountStatus } from './queueCounts'
import { useQueueCounts } from './useQueueCounts'

function response(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(payload),
  } as Response
}

function Probe() {
  const counts = useQueueCounts(['records_officer'])
  return (
    <>
      <output aria-label="team count">{counts.data?.team ?? 'loading'}</output>
      <QueueCountStatus counts={counts.data} />
    </>
  )
}

function renderProbe() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}><Probe /></QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('polls lightweight counts only while visible, refreshes on focus, and announces changes', async () => {
  let team = 2
  const fetchMock = vi.fn(async () => response({
    team,
    personal: 1,
    review: 0,
    inbox_needs_attention: 1,
    inbox_delivery_exceptions: 0,
  }))
  vi.stubGlobal('fetch', fetchMock)
  renderProbe()

  expect(await screen.findByText('2')).toBeInTheDocument()
  expect(screen.getByRole('status', { name: 'Queue count updates' })).toHaveTextContent('')

  team = 3
  await act(async () => vi.advanceTimersByTime(30_000))
  await waitFor(() => expect(screen.getByLabelText('team count')).toHaveTextContent('3'))
  expect(screen.getByRole('status', { name: 'Queue count updates' })).toHaveTextContent('Queue counts updated')

  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'hidden',
  })
  document.dispatchEvent(new Event('visibilitychange'))
  const hiddenCalls = fetchMock.mock.calls.length
  await act(async () => vi.advanceTimersByTime(60_000))
  expect(fetchMock).toHaveBeenCalledTimes(hiddenCalls)

  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  })
  document.dispatchEvent(new Event('visibilitychange'))
  window.dispatchEvent(new Event('focus'))
  await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(hiddenCalls))
})
