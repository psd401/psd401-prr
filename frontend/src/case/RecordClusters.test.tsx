// ABOUTME: The cluster view lists every cluster with its size, samples, and AI
// ABOUTME: majority, and opening a sample hands its id back to the page.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { RecordClusters } from './RecordClusters'

afterEach(() => vi.unstubAllGlobals())

it('lists clusters with size, samples, and the AI majority', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return {
      ok: true, status: 200,
      text: async () => JSON.stringify({
        group_by: 'sender', cluster_count: 3,
        clusters: [
          {
            key: 'list:news.example', label: 'news.example', size: 4,
            ai_majority: { label: 'non_responsive', count: 3 },
            samples: [{
              id: 'r1', original_filename: 'newsletter.eml', review_state: 'unreviewed',
              advisory_suggested_state: 'non_responsive', advisory_confidence: 0.95,
            }],
            record_item_ids: ['r1', 'r2', 'r3', 'r4'],
          },
          {
            key: '', label: 'No sender', size: 1, ai_majority: null,
            samples: [{
              id: 'r5', original_filename: 'scan.pdf', review_state: 'unreviewed',
              advisory_suggested_state: null, advisory_confidence: null,
            }],
            record_item_ids: ['r5'],
          },
        ],
      }),
    } as Response
  }))
  const onOpen = vi.fn()
  const onClusterBy = vi.fn()
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RecordClusters
        filters={new URLSearchParams({ request: 'PRR-2026-0001' })}
        filterKey={['PRR-2026-0001']}
        clusterBy="sender"
        onClusterBy={onClusterBy}
        onOpen={onOpen}
      />
    </QueryClientProvider>,
  )

  expect(await screen.findByText('news.example')).toBeInTheDocument()
  expect(screen.getByText('4 records')).toBeInTheDocument()
  expect(screen.getByText('AI majority: Non-responsive (3 of 4)')).toBeInTheDocument()
  expect(screen.getByText('No AI suggestions')).toBeInTheDocument()
  expect(screen.getByText('Showing the 2 largest of 3 clusters.')).toBeInTheDocument()
  expect(urls[0]).toBe('/api/staff/record-items/clusters/?request=PRR-2026-0001&group_by=sender')

  await userEvent.click(screen.getByRole('button', { name: 'newsletter.eml' }))
  expect(onOpen).toHaveBeenCalledWith('r1')
  await userEvent.selectOptions(screen.getByLabelText('Cluster by'), 'thread')
  expect(onClusterBy).toHaveBeenCalledWith('thread')
})
