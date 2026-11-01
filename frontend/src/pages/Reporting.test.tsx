// ABOUTME: JLARC reporting stays a human-reviewed transfer workflow.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { Reporting } from './Reporting'

function response(payload: unknown, ok = true, status = 200) {
  return { ok, status, text: async () => JSON.stringify(payload) }
}

const report = {
  reporting_year: 2025,
  period: { start: '2025-01-01', end_exclusive: '2026-01-01' },
  guidance: {
    statute: 'RCW 40.14.026(4)-(6)',
    metric_guidance_version: 'January 2025',
    submission: 'Human transfer to the JLARC online reporting system; no automatic submission.',
  },
  supplement: { revision: 1, methodology_notes: 'Finance year end.', created_by: 'officer' },
  baseline: {
    open_at_start: 2,
    open_at_start_closed_during_period: 1,
    received_during_period: 10,
    received_during_period_closed_during_period: 8,
    total_closed_during_period: 9,
  },
  metrics: Object.fromEntries(Array.from({ length: 15 }, (_, index) => [
    String(index + 1),
    { label: `Metric ${index + 1}`, value: index + 1 },
  ])),
  quality_warnings: ['Fulfillment format is unclassified for closed requests: PRR-2025-0001'],
  request_log: [{
    request_number: 'PRR-2025-0001',
    requester_identity: 'requester@example.org',
    received_date: '2025-01-02',
    original_request: 'Board records.',
    records_produced_description: '',
    records_withheld_description: '',
    withholding_reasons: [],
    final_disposition_date: '2025-01-06',
  }],
  data_sha256: 'a'.repeat(64),
  generated_at: '2026-08-02T20:00:00Z',
}

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })}>
      <Reporting roles={['records_officer']} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('renders all 15 metrics, data warnings, and a manual-transfer worksheet', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(report)))
  renderPage()

  expect(await screen.findByText('2025 JLARC worksheet')).toBeInTheDocument()
  expect(screen.getByText(/unclassified for closed requests/i)).toBeInTheDocument()
  expect(screen.getAllByRole('heading', { name: /^Metric \d+$/ })).toHaveLength(15)
  const download = screen.getByRole('link', { name: 'Download transfer worksheet' })
  expect(download).toHaveAttribute('href', '/api/staff/jlarc-reports/2025/export-csv/')
  expect(screen.getByText('a'.repeat(64))).toBeInTheDocument()
})

it('records finance, risk, and records-management inputs before generating', async () => {
  let hasSupplement = false
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/jlarc-reports/2025/supplement/') {
      hasSupplement = true
      return response({ revision: 1 })
    }
    if (hasSupplement) return response(report)
    return response(['A Records Officer must record the annual supplement.'], false, 400)
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  await user.click(await screen.findByRole('button', { name: 'Record annual inputs' }))
  const dialog = screen.getByRole('dialog', { name: 'Record JLARC annual inputs' })
  await user.type(within(dialog).getByLabelText('Staff compensation cost'), '100000')
  await user.type(within(dialog).getByLabelText('Methodology notes'), 'Finance, HR, risk, and records office year-end ledgers.')
  await user.click(within(dialog).getByRole('button', { name: 'Save annual inputs' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/jlarc-reports/2025/supplement/',
    expect.objectContaining({ method: 'POST' }),
  )
  expect(await screen.findByText('2025 JLARC worksheet')).toBeInTheDocument()
})

it('adds an append-only request classification without asking the requester', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).endsWith('/jlarc-classification/')) return response({ revision: 1 })
    return response(report)
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  const row = (await screen.findByText('PRR-2025-0001')).closest('tr')!
  await user.click(within(row).getByRole('button', { name: 'Classify' }))
  const dialog = screen.getByRole('dialog', { name: 'Classify request for JLARC' })
  expect(within(dialog).getByText(/do not contact or prompt the requester/i)).toBeInTheDocument()
  await user.type(within(dialog).getByLabelText('Records produced description'), 'Responsive board minutes.')
  await user.type(within(dialog).getByLabelText('Records withheld description'), 'None.')
  await user.type(within(dialog).getByLabelText('Classification reason'), 'Final-disposition classification from production evidence.')
  await user.click(within(dialog).getByRole('button', { name: 'Save classification' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/requests/PRR-2025-0001/jlarc-classification/',
    expect.objectContaining({ method: 'POST' }),
  )
})
