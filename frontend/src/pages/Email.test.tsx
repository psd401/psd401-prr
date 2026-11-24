import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import { ToastProvider } from '../ui'
import { Email } from './Email'

function response(payload: unknown): Response {
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) } as Response
}

function page(results: unknown[]) {
  return { count: results.length, next: null, previous: null, results }
}

const counts = {
  inbox_unmatched: 24,
  inbox_quarantined: 3,
  inbox_delivery_exceptions: 0,
}

const heldReceipt = {
  id: '11111111-1111-4111-8111-111111111111',
  received_sender: null,
  received_subject: '',
  work_item_id: 'work-held',
  work_item_version: 1,
  correspondence_id: null,
  parsing_status: 'quarantined',
  matching_status: 'quarantined',
  match_method: '',
  candidate_request_number: null,
  matched_request_number: null,
  quarantined: true,
  quarantine_reason_code: 'malformed_message',
  parsing_error_code: 'malformed_message',
  received_at: '2026-10-29T14:10:41-07:00',
  created_at: '2026-10-29T14:10:41-07:00',
  completed_at: '2026-10-29T14:10:41-07:00',
  attachment_count: 0,
  downloadable_attachment_count: 0,
  quarantined_attachment_count: 0,
  version: 1,
}

function renderEmail(roles: StaffRole[], entry = '/email') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route element={<Outlet context={counts} />}>
              <Route path="*" element={<Email roles={roles} />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

it('opens on mail with no matching request, with plain-word tabs and counts', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([]))
  }))

  renderEmail(['records_officer'])

  expect(await screen.findByRole('heading', { name: 'Email' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^No matching request/ }))
    .toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: /^No matching request/ })).toHaveTextContent('24')
  expect(screen.getByRole('button', { name: /^Couldn't be read/ })).toHaveTextContent('3')
  expect(screen.getByRole('button', { name: /^Bounced/ })).toBeInTheDocument()
  await waitFor(() =>
    expect(urls.some((url) => url.includes('queue=unmatched'))).toBe(true))
})

it('shows a held email as plain text so it can actually be reviewed', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/readable/')) {
      return response({
        headers: [['From', 'Dana Doe <dana@example.org>'], ['Subject', 'Fwd: Data request']],
        body: 'Please see the forwarded request.',
        attachments: ['questionnaire.csv'],
        raw_source: false,
        truncated: false,
      })
    }
    if (url.includes(`/inbound-correspondence-receipts/${heldReceipt.id}/`)) {
      return response({ ...heldReceipt, received_body: '', received_headers: {}, attachments: [], delivery_notifications: [], references: [], envelope_recipients: [], received_recipients: [], in_reply_to: [] })
    }
    return response(page([heldReceipt]))
  }))
  const user = userEvent.setup()

  renderEmail(['records_officer'], '/email?tab=unreadable')

  await user.click(await screen.findByRole('button', { name: /^Review email received/ }))

  expect(await screen.findByText('Please see the forwarded request.')).toBeInTheDocument()
  expect(screen.getByText('Dana Doe <dana@example.org>')).toBeInTheDocument()
  expect(screen.getByText(/questionnaire\.csv/)).toBeInTheDocument()
})

it('gives an admin only the bounced letters', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([]))))

  renderEmail(['admin'])

  expect(await screen.findByRole('button', { name: /^Bounced/ }))
    .toHaveAttribute('aria-pressed', 'true')
  expect(screen.queryByRole('button', { name: /^No matching request/ })).not.toBeInTheDocument()
  expect(await screen.findByText('No letters have bounced.')).toBeInTheDocument()
})
