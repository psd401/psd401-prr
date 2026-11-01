import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import type { CorrespondenceDetail, CorrespondenceSummary } from '../correspondence'
import { ToastProvider } from '../ui'
import { MessageWorkspace } from './MessageWorkspace'
import { at } from '../test/at'

function fakeResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

const summary = (id: string, subject = `Subject ${id}`): CorrespondenceSummary => ({
  id,
  request_number: 'PRR-2026-0001',
  direction: 'inbound',
  kind: 'general',
  status: 'received',
  subject,
  snippet: `Snippet ${id}`,
  automated: false,
  created_at: `2026-08-20T12:00:${id.padStart(2, '0')}Z`,
  approved_at: null,
  queued_at: null,
  sent_at: null,
  received_at: `2026-08-20T12:00:${id.padStart(2, '0')}Z`,
  matched_at: null,
  failed_at: null,
  cancelled_at: null,
  failure_category: '',
  version: 1,
  recipient_count: 0,
  attachment_count: 0,
  delivery_status: null,
  matching_status: 'matched',
  resulting_status_event_id: null,
  inbound_receipt_id: null,
  work_item: null,
  case_context: null,
})

const detail = (row: CorrespondenceSummary): CorrespondenceDetail => ({
  ...row,
  draft_subject: '',
  draft_body: '',
  draft_metadata: {},
  approved_subject: '',
  approved_body: '',
  approved_metadata: {},
  received_subject: row.subject,
  received_body: `Full body ${row.id}`,
  received_sender: 'sender@example.org',
  received_recipients: ['records@example.org'],
  template_identifier: '',
  template_version: '',
  created_by: null,
  approved_by: null,
  queued_by: null,
  matched_by: null,
  rfc_message_id: '',
  provider: 'gmail',
  provider_account: 'default',
  provider_message_id: row.id,
  provider_thread_id: '',
  failure_description: '',
  replaces_id: null,
  resulting_status: null,
  recipients: [],
  attachments: [],
  denial_exemption_citations: [],
  delivery: null,
})

function LocationProbe() {
  const location = useLocation()
  return <output aria-label="location">{location.search}</output>
}

function renderWorkspace({
  initialEntry = '/requests/PRR-2026-0001',
  requestNumber = 'PRR-2026-0001',
  roles = ['records_officer'] as StaffRole[],
  username = 'local-test-user',
} = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  // The saved-search scope now comes from the signed-in user rather than a
  // prop, so seed the session the component reads instead of teaching every
  // fetch mock in this file about /api/staff/auth/me.
  client.setQueryData(['me'], {
    authenticated: true,
    username,
    email: `${username}@example.org`,
    first_name: 'Test',
    last_name: 'User',
    is_superuser: false,
    roles,
  })
  return {
    ...render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <MessageWorkspace
            requestNumber={requestNumber}
            roles={roles}
           
            renderDetail={(row) => <p>Mounted detail: {row.received_body}</p>}
          />
          <LocationProbe />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
    ),
    queryClient: client,
  }
}

beforeEach(() => {
  document.cookie = 'csrftoken=test-token'
  sessionStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('MessageWorkspace', () => {
  it('loads one bounded summary page, then one selected detail and later pages only on demand', async () => {
    const first = Array.from({ length: 50 }, (_, index) => summary(String(index + 1)))
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse({ count: 51, next: 'http://django:8000/api/staff/correspondence/?cursor=next', previous: null, results: first })
      }
      if (url === '/api/staff/correspondence/1/?request=PRR-2026-0001') return fakeResponse(detail(at(first, 0)))
      if (url === '/api/staff/correspondence/?cursor=next') {
        return fakeResponse({ count: 51, next: null, previous: null, results: [summary('51')] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderWorkspace()

    expect(await screen.findByText('Subject 1')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/Mounted detail/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open Subject 1' }))
    expect(await screen.findByText('Mounted detail: Full body 1')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(screen.getByLabelText('location')).toHaveTextContent('message=1')

    await user.click(screen.getByRole('button', { name: 'Load more messages' }))
    expect(await screen.findByText('Subject 51')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('keeps only one 50-row summary page mounted while navigating loaded pages', async () => {
    const pages = [0, 1, 2].map((page) => Array.from(
      { length: 50 },
      (_, index) => summary(String((page * 50) + index + 1)),
    ))
    let firstPageRequests = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        firstPageRequests += 1
        return fakeResponse({
          count: 150,
          next: 'http://django:8000/api/staff/correspondence/?cursor=page-2',
          previous: null,
          results: firstPageRequests === 1
            ? pages[0]
            : [summary('1', 'Refreshed Subject 1'), ...at(pages, 0).slice(1)],
        })
      }
      if (url === '/api/staff/correspondence/?cursor=page-2') {
        return fakeResponse({
          count: 150,
          next: 'http://django:8000/api/staff/correspondence/?cursor=page-3',
          previous: null,
          results: pages[1],
        })
      }
      if (url === '/api/staff/correspondence/?cursor=page-3') {
        return fakeResponse({
          count: 150,
          next: null,
          previous: null,
          results: pages[2],
        })
      }
      if (url === '/api/staff/correspondence/threads/?request=PRR-2026-0001') {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWorkspace()
    expect(await screen.findByText('Showing 1–50 of 150 messages')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Open Subject/ })).toHaveLength(50)

    await user.click(screen.getByRole('button', { name: 'Load more messages' }))
    expect(await screen.findByText('Showing 51–100 of 150 messages')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Open Subject/ })).toHaveLength(50)
    expect(screen.queryByRole('button', { name: 'Open Subject 1' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Load more messages' }))
    expect(await screen.findByText('Showing 101–150 of 150 messages')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Open Subject/ })).toHaveLength(50)

    await user.click(screen.getByRole('button', { name: 'Show newer messages' }))
    expect(await screen.findByText('Showing 51–100 of 150 messages')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Open Subject/ })).toHaveLength(50)

    await user.click(screen.getByRole('button', { name: 'Threads' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/correspondence/threads/?request=PRR-2026-0001',
      expect.any(Object),
    ))
    await user.click(screen.getByRole('button', { name: 'Messages' }))
    expect(await screen.findByText('Showing 1–50 of 150 messages')).toBeInTheDocument()
    expect(await screen.findByText('Refreshed Subject 1')).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => (
      String(input).startsWith('/api/staff/correspondence/?')
    ))).toHaveLength(4)
  })

  it('keeps only one 50-row thread page mounted while navigating loaded pages', async () => {
    const pages = [0, 1, 2].map((page) => Array.from(
      { length: 50 },
      (_, index) => ({
        id: `thread-${(page * 50) + index + 1}`,
        evidence_id: `evidence-${(page * 50) + index + 1}`,
        request_number: 'PRR-2026-0001',
        subject: `Thread ${(page * 50) + index + 1}`,
        message_count: 1,
        latest_at: '2026-08-20T12:00:00Z',
      }),
    ))
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/threads/?request=PRR-2026-0001') {
        return fakeResponse({
          count: 150,
          next: 'http://django:8000/api/staff/correspondence/threads/?cursor=page-2',
          previous: null,
          results: pages[0],
        })
      }
      if (url === '/api/staff/correspondence/threads/?cursor=page-2') {
        return fakeResponse({
          count: 150,
          next: 'http://django:8000/api/staff/correspondence/threads/?cursor=page-3',
          previous: null,
          results: pages[1],
        })
      }
      if (url === '/api/staff/correspondence/threads/?cursor=page-3') {
        return fakeResponse({ count: 150, next: null, previous: null, results: pages[2] })
      }
      if (
        url
        === '/api/staff/correspondence/thread-messages/?request=PRR-2026-0001&thread=thread-51'
      ) {
        return fakeResponse({ count: 1, next: null, previous: null, results: [summary('51')] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWorkspace({
      initialEntry: '/requests/PRR-2026-0001?message_mode=threads',
    })
    expect(await screen.findByText('Showing 1–50 of 150 threads')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Thread / })).toHaveLength(50)

    await user.click(screen.getByRole('button', { name: 'Load more threads' }))
    expect(await screen.findByText('Showing 51–100 of 150 threads')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Thread / })).toHaveLength(50)
    expect(screen.queryByRole('button', { name: /Thread 1\b/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Load more threads' }))
    expect(await screen.findByText('Showing 101–150 of 150 threads')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Thread / })).toHaveLength(50)

    await user.click(screen.getByRole('button', { name: 'Show newer threads' }))
    expect(await screen.findByText('Showing 51–100 of 150 threads')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^Thread 51/ }))
    await screen.findByText('Subject 51')
    await user.click(screen.getByRole('button', { name: 'Back to thread summaries' }))
    expect(await screen.findByText('Showing 51–100 of 150 threads')).toBeInTheDocument()
  })

  it('does not let delayed thread pagination overwrite a newer filter generation', async () => {
    let resolveOlder!: (response: Response) => void
    const delayedOlder = new Promise<Response>((resolve) => {
      resolveOlder = resolve
    })
    const thread = (id: string) => ({
      id: `thread-${id}`,
      evidence_id: `evidence-${id}`,
      request_number: 'PRR-2026-0001',
      subject: `Thread ${id}`,
      message_count: 1,
      latest_at: '2026-08-20T12:00:00Z',
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/threads/?request=PRR-2026-0001') {
        return fakeResponse({
          count: 2,
          next: 'http://django:8000/api/staff/correspondence/threads/?cursor=older',
          previous: null,
          results: [thread('1')],
        })
      }
      if (url === '/api/staff/correspondence/threads/?cursor=older') return delayedOlder
      if (
        url
        === '/api/staff/correspondence/threads/?request=PRR-2026-0001&direction=inbound'
      ) {
        return fakeResponse({ count: 1, next: null, previous: null, results: [thread('inbound')] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWorkspace({ initialEntry: '/requests/PRR-2026-0001?message_mode=threads' })
    await screen.findByRole('button', { name: /^Thread 1/ })
    await user.click(screen.getByRole('button', { name: 'Load more threads' }))
    await user.selectOptions(screen.getByLabelText('Direction'), 'inbound')
    await screen.findByRole('button', { name: /^Thread inbound/ })
    await user.selectOptions(screen.getByLabelText('Direction'), '')
    await screen.findByRole('button', { name: /^Thread 1/ })

    resolveOlder(fakeResponse({ count: 2, next: null, previous: null, results: [thread('2')] }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Load more threads' })).toBeEnabled()
    })

    expect(screen.getByRole('button', { name: /^Thread 1/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Thread 2/ })).not.toBeInTheDocument()
  })

  it('does not let delayed pagination overwrite a newer query scope', async () => {
    let resolveOlder!: (response: Response) => void
    let firstPageRequests = 0
    const delayedOlder = new Promise<Response>((resolve) => {
      resolveOlder = resolve
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        firstPageRequests += 1
        return fakeResponse({
          count: 51,
          next: 'http://django:8000/api/staff/correspondence/?cursor=older',
          previous: null,
          results: [summary(
            '1',
            firstPageRequests === 1 ? 'Subject 1' : 'Refreshed Subject 1',
          )],
        })
      }
      if (url === '/api/staff/correspondence/?cursor=older') return delayedOlder
      if (url.includes('direction=inbound')) {
        return fakeResponse({ count: 1, next: null, previous: null, results: [summary('inbound')] })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWorkspace()
    await screen.findByText('Subject 1')
    await user.click(screen.getByRole('button', { name: 'Load more messages' }))
    await user.selectOptions(screen.getByLabelText('Direction'), 'inbound')
    await screen.findByText('Subject inbound')
    await user.selectOptions(screen.getByLabelText('Direction'), '')
    await screen.findByText('Refreshed Subject 1')

    resolveOlder(fakeResponse({ count: 51, next: null, previous: null, results: [summary('51')] }))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Load more messages' })).toBeEnabled()
    })

    await waitFor(() => {
      expect(screen.getByText('Refreshed Subject 1')).toBeInTheDocument()
      expect(screen.queryByText('Subject 51')).not.toBeInTheDocument()
    })
    expect(firstPageRequests).toBe(2)
  })

  it('keeps search text out of URLs and sends it in a bounded POST body', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/?')) {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (url === '/api/staff/staff-users/') {
        return fakeResponse({
          count: 1,
          next: null,
          previous: null,
          results: [{ id: 7, username: 'officer', display_name: 'Records Officer' }],
        })
      }
      if (url === '/api/staff/correspondence/search/' && init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toMatchObject({
          request: 'PRR-2026-0001',
          query: 'student medical phrase',
          direction: 'inbound',
          assignee: '7',
        })
        return fakeResponse({ count: 1, next: null, previous: null, results: [summary('7')] })
      }
      throw new Error(`Unexpected fetch: ${init?.method ?? 'GET'} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderWorkspace()
    await screen.findByText('No messages match these filters.')
    await user.selectOptions(screen.getByLabelText('Direction'), 'inbound')
    await user.click(screen.getByLabelText('Assignee'))
    await screen.findByRole('option', { name: 'Records Officer' })
    await user.selectOptions(screen.getByLabelText('Assignee'), '7')
    expect(screen.getByRole('option', {
      name: 'Awaiting reconciliation',
    })).toBeInTheDocument()
    await user.type(screen.getByLabelText('Search message content'), 'student medical phrase')
    await user.click(screen.getByRole('button', { name: 'Search messages' }))

    expect(await screen.findByText('Subject 7')).toBeInTheDocument()
    expect(screen.getByLabelText('location')).toHaveTextContent('message_direction=inbound')
    expect(screen.getByLabelText('location')).not.toHaveTextContent('student')
  })

  it('shows personal read state separately from shared disposition and records no-action reasons', async () => {
    const row = summary('9')
    row.work_item = {
      id: 'work-9',
      work_kind: 'inbound_reply',
      assignee_id: null,
      assignee_username: null,
      disposition: 'needs_attention',
      version: 1,
      is_read: false,
      read_at: null,
    }
    const full = detail(row)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/?')) {
        return fakeResponse({ count: 1, next: null, previous: null, results: [row] })
      }
      if (url === '/api/staff/correspondence/9/?request=PRR-2026-0001') return fakeResponse(full)
      if (url === '/api/staff/work-items/work-9/read/' && init?.method === 'POST') {
        return fakeResponse({ ...row.work_item, is_read: true })
      }
      if (url === '/api/staff/work-items/work-9/no-action-required/' && init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toMatchObject({
          category: 'informational_only',
          reason: 'Informational copy retained with no response required.',
        })
        return fakeResponse({ ...row.work_item, disposition: 'no_action_required', version: 2 })
      }
      throw new Error(`Unexpected fetch: ${init?.method ?? 'GET'} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    const { queryClient } = renderWorkspace()
    queryClient.setQueryData(
      ['case-workspace-summary', 'PRR-2026-0001'],
      { counts: { new: 1, needs_attention: 1 } },
    )
    queryClient.setQueryData(['work-item-counts'], { inbox_new: 1 })
    await user.click(await screen.findByRole('button', { name: 'Open Subject 9' }))
    expect(await screen.findByText('Personal: Unread')).toBeInTheDocument()
    expect(screen.getByText('Team: Needs attention')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Mark read' }))
    expect(await screen.findByText('Personal: Read')).toBeInTheDocument()
    expect(queryClient.getQueryState([
      'case-workspace-summary',
      'PRR-2026-0001',
    ])?.isInvalidated).toBe(true)
    expect(queryClient.getQueryState(['work-item-counts'])?.isInvalidated).toBe(true)
    await user.click(screen.getByRole('button', { name: 'No action required' }))
    await user.selectOptions(screen.getByLabelText('No-action category'), 'informational_only')
    await user.type(
      screen.getByLabelText(/Why no team action is required/),
      'Informational copy retained with no response required.',
    )
    await user.click(screen.getByRole('button', { name: 'Record no action' }))
    expect(await screen.findByText('Team: No action required')).toBeInTheDocument()
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([input]) =>
        String(input).startsWith('/api/staff/correspondence/?request='),
      ).length).toBeGreaterThan(1)
      expect(fetchMock.mock.calls.filter(([input]) =>
        String(input).startsWith('/api/staff/correspondence/9/?request='),
      ).length).toBeGreaterThan(1)
    })
  })

  it('keeps an async work mutation bound to the message that started it', async () => {
    const first = summary('31')
    const second = summary('32')
    first.work_item = {
      id: 'work-31',
      work_kind: 'inbound_reply',
      assignee_id: null,
      assignee_username: null,
      disposition: 'needs_attention',
      version: 1,
      is_read: false,
      read_at: null,
    }
    second.work_item = {
      id: 'work-32',
      work_kind: 'inbound_reply',
      assignee_id: null,
      assignee_username: null,
      disposition: 'waiting',
      version: 1,
      is_read: false,
      read_at: null,
    }
    let resolveRead!: (response: Response) => void
    const pendingRead = new Promise<Response>((resolve) => {
      resolveRead = resolve
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/?')) {
        return fakeResponse({ count: 2, next: null, previous: null, results: [first, second] })
      }
      if (url.includes('/api/staff/correspondence/31/')) return fakeResponse(detail(first))
      if (url.includes('/api/staff/correspondence/32/')) return fakeResponse(detail(second))
      if (url === '/api/staff/work-items/work-31/read/' && init?.method === 'POST') {
        return pendingRead
      }
      throw new Error(`Unexpected fetch: ${init?.method ?? 'GET'} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWorkspace()
    await user.click(await screen.findByRole('button', { name: 'Open Subject 31' }))
    await screen.findByText('Team: Needs attention')
    await user.click(screen.getByRole('button', { name: 'Mark read' }))
    await user.click(screen.getByRole('button', { name: 'Open Subject 32' }))
    expect(await screen.findByText('Team: Waiting')).toBeInTheDocument()

    first.work_item = { ...first.work_item, is_read: true }
    resolveRead(fakeResponse(first.work_item))

    await waitFor(() => {
      expect(screen.getByText('Personal: Unread')).toBeInTheDocument()
      expect(screen.getByText('Team: Waiting')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Mark read' })).toBeInTheDocument()
  })

  it('stages newly arrived messages behind a stable banner until applied', async () => {
    let firstPage = [summary('1')]
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/?')) {
        return fakeResponse({ count: firstPage.length, next: null, previous: null, results: firstPage })
      }
      if (url === '/api/staff/correspondence/1/?request=PRR-2026-0001') {
        return fakeResponse(detail(summary('1')))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderWorkspace()
    expect(await screen.findByText('Subject 1')).toBeInTheDocument()
    const originalFocus = screen.getByRole('button', { name: 'Open Subject 1' })
    await user.click(originalFocus)
    await screen.findByText('Mounted detail: Full body 1')
    expect(originalFocus).toHaveAttribute('aria-pressed', 'true')
    firstPage = [summary('2'), summary('1')]
    window.dispatchEvent(new Event('focus'))

    expect(await screen.findByRole('button', { name: 'Show 1 new message' })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'New message updates' })).toHaveTextContent(
      '1 new message is available.',
    )
    expect(originalFocus).toHaveFocus()
    expect(originalFocus).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByText('Subject 2')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show 1 new message' }))
    expect(await screen.findByText('Subject 2')).toBeInTheDocument()
  })

  it('discards a staged-arrival banner when the query scope changes', async () => {
    let firstPage = [summary('1')]
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/?')) {
        return fakeResponse({ count: firstPage.length, next: null, previous: null, results: firstPage })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWorkspace()
    await screen.findByText('Subject 1')
    firstPage = [summary('2'), summary('1')]
    window.dispatchEvent(new Event('focus'))
    expect(await screen.findByRole('button', { name: 'Show 1 new message' })).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Direction'), 'inbound')

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Show 1 new message' })).not.toBeInTheDocument()
    })
  })

  it('detects a new arrival by count when oldest-first keeps the first row stable', async () => {
    let oldestPage = [summary('1')]
    let newestPage = [summary('1')]
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('sort=oldest')) {
        return fakeResponse({ count: oldestPage.length, next: null, previous: null, results: oldestPage })
      }
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse({ count: newestPage.length, next: null, previous: null, results: newestPage })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderWorkspace({
      initialEntry: '/requests/PRR-2026-0001?message_sort=oldest',
    })
    expect(await screen.findByText('Subject 1')).toBeInTheDocument()
    oldestPage = [summary('1'), summary('2')]
    newestPage = [summary('2'), summary('1')]
    window.dispatchEvent(new Event('focus'))

    expect(await screen.findByRole('button', { name: 'Show 1 new message' })).toBeInTheDocument()
    expect(screen.queryByText('Subject 2')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show 1 new message' }))
    expect(await screen.findByText('Subject 2')).toBeInTheDocument()
    expect(screen.getByLabelText('Sort order')).toHaveValue('newest')
  })

  it('stages the first arrival from an empty result without flagging a reorder', async () => {
    let firstPage: CorrespondenceSummary[] = []
    const fetchMock = vi.fn(async () => fakeResponse({
      count: firstPage.length,
      next: null,
      previous: null,
      results: firstPage,
    }))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWorkspace()
    expect(await screen.findByText('No messages match these filters.')).toBeInTheDocument()
    firstPage = [summary('1')]
    window.dispatchEvent(new Event('focus'))
    await user.click(await screen.findByRole('button', { name: 'Show 1 new message' }))
    expect(await screen.findByText('Subject 1')).toBeInTheDocument()

    const callsBeforeReorder = fetchMock.mock.calls.length
    firstPage = [summary('2')]
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBeforeReorder)
    })
    expect(screen.queryByRole('button', { name: /Show .* new message/ })).not.toBeInTheDocument()
  })

  it('does not restore a prior account search from origin-wide browser storage', async () => {
    sessionStorage.setItem(
      'message-search:first-officer:PRR-2026-0001',
      'prior officer sensitive query',
    )
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse({
      count: 0,
      next: null,
      previous: null,
      results: [],
    })))

    renderWorkspace({ username: 'second-officer' })

    expect(await screen.findByText('No messages match these filters.')).toBeInTheDocument()
    expect(screen.getByLabelText('Search message content')).toHaveValue('')
  })

  it('restores an applied search for the same actor after a page remount', async () => {
    sessionStorage.setItem(
      'message-search:records-officer:PRR-2026-0001',
      'stable refresh phrase',
    )
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/search/' && init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toMatchObject({
          request: 'PRR-2026-0001',
          query: 'stable refresh phrase',
        })
        return fakeResponse({ count: 1, next: null, previous: null, results: [summary('6')] })
      }
      throw new Error(`Unexpected fetch: ${init?.method ?? 'GET'} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWorkspace({ username: 'records-officer' })

    expect(await screen.findByText('Subject 6')).toBeInTheDocument()
    expect(screen.getByLabelText('Search message content')).toHaveValue(
      'stable refresh phrase',
    )
  })

  it('scopes a URL-selected detail to the request workspace', async () => {
    const foreignId = 'foreign-message'
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse({ count: 0, next: null, previous: null, results: [] })
      }
      if (
        url
        === `/api/staff/correspondence/${foreignId}/?request=PRR-2026-0001`
      ) {
        return fakeResponse({ detail: 'Not found.' }, 404)
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWorkspace({
      initialEntry: `/requests/PRR-2026-0001?message=${foreignId}`,
    })

    expect(await screen.findByText('Not found.')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/staff/correspondence/${foreignId}/?request=PRR-2026-0001`,
      expect.anything(),
    )
    expect(screen.queryByText(/Mounted detail/)).not.toBeInTheDocument()
  })

  it('does not expose inbound work controls to a liaison', async () => {
    const row = summary('8')
    row.work_item = {
      id: 'officer-work-8',
      work_kind: 'inbound_reply',
      assignee_id: 1,
      assignee_username: 'officer',
      disposition: 'needs_attention',
      version: 1,
      is_read: false,
      read_at: null,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/staff/correspondence/?')) {
        return fakeResponse({ count: 1, next: null, previous: null, results: [row] })
      }
      if (url.includes('/api/staff/correspondence/8/')) {
        return fakeResponse(detail(row))
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }))
    const user = userEvent.setup()

    renderWorkspace({ roles: ['liaison'] })
    await user.click(await screen.findByRole('button', { name: 'Open Subject 8' }))
    expect(await screen.findByText('Mounted detail: Full body 8')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mark read' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Personal read state')).not.toBeInTheDocument()
  })

  it('resets sensitive search and checked work before SPA request navigation', async () => {
    const first = summary('11')
    first.work_item = {
      id: 'work-11',
      work_kind: 'inbound_reply',
      assignee_id: null,
      assignee_username: null,
      disposition: 'needs_attention',
      version: 1,
      is_read: false,
      read_at: null,
    }
    const second = {
      ...summary('22'),
      request_number: 'PRR-2026-0002',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/staff/correspondence/?request=PRR-2026-0001') {
        return fakeResponse({ count: 1, next: null, previous: null, results: [first] })
      }
      if (url === '/api/staff/correspondence/?request=PRR-2026-0002') {
        return fakeResponse({ count: 1, next: null, previous: null, results: [second] })
      }
      if (url === '/api/staff/correspondence/search/' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        if (body.request === 'PRR-2026-0002') {
          throw new Error('Search state leaked into the next request.')
        }
        return fakeResponse({ count: 1, next: null, previous: null, results: [first] })
      }
      throw new Error(`Unexpected fetch: ${init?.method ?? 'GET'} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    function Switcher() {
      const [requestNumber, setRequestNumber] = useState('PRR-2026-0001')
      return (
        <>
          <button type="button" onClick={() => setRequestNumber('PRR-2026-0002')}>
            Switch request
          </button>
          <MessageWorkspace
            requestNumber={requestNumber}
            roles={['records_officer']}
            renderDetail={(row) => <p>{row.received_body}</p>}
          />
        </>
      )
    }
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <MemoryRouter>
            <Switcher />
          </MemoryRouter>
        </ToastProvider>
      </QueryClientProvider>,
    )
    const user = userEvent.setup()

    await user.type(
      await screen.findByLabelText('Search message content'),
      'request A private phrase',
    )
    await user.click(screen.getByRole('button', { name: 'Search messages' }))
    await user.click(await screen.findByRole('checkbox', {
      name: 'Select Subject 11 for bulk metadata',
    }))
    expect(screen.getByText('1 selected (maximum 50)')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Switch request' }))

    expect(await screen.findByText('Subject 22')).toBeInTheDocument()
    expect(screen.getByLabelText('Search message content')).toHaveValue('')
    expect(screen.queryByText('1 selected (maximum 50)')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, init]) => {
      if (!init?.body) return false
      return JSON.parse(String(init.body)).request === 'PRR-2026-0002'
    })).toBe(false)
  })
})
