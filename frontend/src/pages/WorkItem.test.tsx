import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { WorkItemDetail } from './WorkItem'

function response(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(payload),
  } as Response
}

const item = {
  id: 'work-1',
  source_type: 'correspondence',
  source_id: 'message-1',
  work_kind: 'correspondence_draft',
  request_number: 'PRR-2026-0042',
  assignee_id: 7,
  assignee_username: 'liaison-one',
  disposition: 'needs_attention',
  version: 1,
  is_read: false,
  read_at: null,
  urgency_date: '2026-08-24',
  next_action: 'Prepare correspondence',
  detail_url: '/work/work-1',
  source_url: '/requests/PRR-2026-0042/messages?message=message-1',
  disposed_at: null,
  created_at: '2026-08-20T10:00:00Z',
  updated_at: '2026-08-23T10:00:00Z',
}

function renderPage(initialEntry = '/work/work-1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    ...render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/work/:id" element={<WorkItemDetail roles={['records_officer']} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
    ),
    queryClient: client,
  }
}

afterEach(() => vi.unstubAllGlobals())

it('loads one exact work item and links to its source context', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/work-items/work-1/') {
      return response({ ...item, events: [] })
    }
    if (String(input) === '/api/staff/work-items/work-1/assignment-targets/') {
      return response([])
    }
    throw new Error(`Unexpected fetch: ${String(input)}`)
  }))

  renderPage('/work/work-1')

  expect(await screen.findByRole('heading', { name: 'Prepare correspondence' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Open source context' })).toHaveAttribute(
    'href',
    '/requests/PRR-2026-0042/messages?message=message-1',
  )
})

it('offers only server-approved assignment targets and persists versioned work metadata', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/staff/work-items/work-1/') {
      return response({ ...item, events: [] })
    }
    if (url === '/api/staff/work-items/work-1/assignment-targets/') {
      return response([{ id: 8, username: 'liaison-two', display_name: 'Liaison Two' }])
    }
    if (url === '/api/staff/work-items/work-1/assignment/') {
      return response({ ...item, assignee_id: 8, assignee_username: 'liaison-two', version: 2 })
    }
    throw new Error(`Unexpected fetch: ${url} ${String(init?.method)}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const event = userEvent.setup()

  const { queryClient } = renderPage('/work/work-1')
  queryClient.setQueryData(
    ['case-workspace-summary', 'PRR-2026-0042'],
    { counts: { new: 1, needs_attention: 1 } },
  )
  await screen.findByRole('heading', { name: 'Prepare correspondence' })
  await event.selectOptions(
    await screen.findByRole('combobox', { name: 'Assignment' }),
    '8',
  )
  await event.click(screen.getByRole('button', { name: 'Save assignment' }))

  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input) === '/api/staff/work-items/work-1/assignment/'
    ))).toBe(true)
  })
  const assignmentCall = fetchMock.mock.calls.find(([input]) => (
    String(input) === '/api/staff/work-items/work-1/assignment/'
  ))
  expect(assignmentCall?.[1]?.method).toBe('POST')
  expect(JSON.parse(String(assignmentCall?.[1]?.body))).toMatchObject({
    assignee_id: 8,
    expected_version: 1,
  })
  expect(queryClient.getQueryState([
    'case-workspace-summary',
    'PRR-2026-0042',
  ])?.isInvalidated).toBe(true)
})
