import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  MemoryRouter,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import { Inbox } from './Inbox'
import { at } from '../test/at'

function response(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(payload),
  } as Response
}

function page(results: unknown[]) {
  return { count: results.length, next: null, previous: null, results }
}

const inboxMessage = {
  id: 'message-1',
  request_number: 'PRR-2026-0042',
  direction: 'inbound',
  kind: 'general',
  status: 'received',
  subject: '',
  snippet: '',
  automated: false,
  created_at: '2026-08-23T10:00:00Z',
  approved_at: null,
  queued_at: null,
  sent_at: null,
  received_at: '2026-08-23T10:00:00Z',
  matched_at: '2026-08-23T10:00:00Z',
  failed_at: null,
  cancelled_at: null,
  failure_category: '',
  version: 1,
  recipient_count: 0,
  attachment_count: 0,
  delivery_status: null,
  matching_status: 'matched',
  resulting_status_event_id: null,
  inbound_receipt_id: 'receipt-1',
  work_item: {
    id: 'work-1',
    work_kind: 'inbound_reply',
    assignee_id: null,
    assignee_username: null,
    disposition: 'needs_attention',
    version: 1,
    is_read: false,
    read_at: null,
  },
  case_context: {
    legal_status: 'received',
    statutory_response_state: 'manual_required',
    statutory_response_due: '2026-09-04',
    email_intake_auto_created: false,
  },
}

const emailIntakeMessage = {
  ...inboxMessage,
  id: 'message-2',
  request_number: 'PRR-2026-0014',
  subject: 'NAGARA webinar: records retention',
  snippet: 'Register today for our fall series.',
  work_item: {
    ...inboxMessage.work_item,
    id: 'work-2',
    work_kind: 'email_intake',
  },
  case_context: {
    legal_status: 'received',
    statutory_response_state: 'manual_required',
    statutory_response_due: '2026-09-04',
    email_intake_auto_created: true,
  },
}

const exception = {
  id: 'work-exception',
  source_type: 'inbound_receipt',
  source_id: 'receipt-exception',
  work_kind: 'delivery_exception',
  request_number: 'PRR-2026-0042',
  assignee_id: null,
  assignee_username: null,
  disposition: 'needs_attention',
  version: 1,
  is_read: false,
  read_at: null,
  urgency_date: null,
  next_action: 'Resolve delivery exception',
  detail_url: '/work/work-exception',
  source_url: '/requests/PRR-2026-0042?workItem=work-exception',
  disposed_at: null,
  created_at: '2026-08-23T10:00:00Z',
  updated_at: '2026-08-23T10:00:00Z',
}

const counts = {
  total: 5,
  unresolved: 5,
  needs_attention: 5,
  waiting: 0,
  handled: 0,
  no_action_required: 0,
  source_excluded: 0,
  personal: 0,
  team: 5,
  unassigned: 5,
  critical: 1,
  recently_changed: 5,
  review: 0,
  inbox_needs_attention: 1,
  inbox_updated_cases: 1,
  inbox_open_cases: 4,
  inbox_awaiting_statutory_response: 2,
  inbox_new: 1,
  inbox_unmatched: 1,
  inbox_quarantined: 0,
  inbox_delivery_exceptions: 1,
  inbox_waiting: 0,
  inbox_handled: 0,
  inbox_no_action: 0,
  inbox_all: 1,
}

/** Reports where the router currently is, for the row-click tests. */
function PathProbe() {
  return <span data-testid="location">{useLocation().pathname}</span>
}

function renderInbox(
  roles: StaffRole[],
  queueCounts = undefined as typeof counts | undefined,
  initialEntries = ['/inbox'],
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>
        <Routes>
          <Route element={<Outlet context={queueCounts} />}>
            <Route
              path="*"
              element={<><PathProbe /><Inbox roles={roles} /></>}
            />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function HistoryProbe() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output aria-label="Current search">{location.search}</output>
      <button type="button" onClick={() => navigate(-1)}>Back</button>
    </>
  )
}

afterEach(() => vi.unstubAllGlobals())

it('uses the bounded message API and links to the exact source context', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/staff/correspondence/?work_state=needs_attention&work_kind=inbound_reply&work_kind=email_intake&case_state=open') {
      return response(page([inboxMessage]))
    }
    if (url === '/api/staff/inbound-correspondence-receipts/?queue=unmatched') {
      return response(page([]))
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()

  renderInbox(['records_officer'], undefined, ['/inbox?view=needs_attention'])

  expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
  // "Open case" opens the case. It used to deep-link into the Messages tab at
  // one message, which dropped an officer into correspondence for a case they
  // had not seen yet; that jump now lives behind "Read in context".
  expect(await screen.findByRole('link', { name: /^Open case / })).toHaveAttribute(
    'href',
    '/requests/PRR-2026-0042',
  )
  await userEvent.setup().click(
    screen.getByRole('button', { name: /^More actions: / }),
  )
  expect(screen.getByRole('link', { name: /^Read in context: / })).toHaveAttribute(
    'href',
    '/requests/PRR-2026-0042/messages?message=message-1',
  )
  expect(screen.queryByText(/sender/i)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Needs a case' }))
  expect(await screen.findByText('No messages are waiting in this queue.'))
    .toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

/**
 * Reach a queue that lives behind "More views".
 *
 * Five decision queues are chips in the command bar; the rest keep their
 * original four-group taxonomy inside a menu. Opening the menu is a real step
 * an officer takes, so the tests take it too rather than asserting against a
 * structure the officer never sees.
 */
/**
 * A role whose whole queue list fits on the bar gets no menu at all: the
 * disclosure is there to tame the officer's seventeen chips, not to hide six.
 */
const caseRow = {
  id: 7,
  number: 'PRR-2026-0042',
  status: 'closed_withdrawn',
  matter_kind: 'public_records',
  matter_kind_display: 'Public records request (RCW 42.56)',
  ferpa_access_due: null,
  subject: 'Stadium project emails',
  subject_source: 'description' as const,
  received_at: '2026-08-01T12:00:00Z',
  statutory_response_due: '2026-08-08',
  estimated_completion: null,
  closed_at: '2026-08-20T12:00:00Z',
  requester_email: 'jane@example.org',
  requester_name: 'Jane Q. Public',
  assigned_officer_username: 'officer',
  initial_response_delivered: true,
  next_due: null,
  next_due_basis: null as
    | 'statutory' | 'estimate' | 'district_target' | 'ferpa' | null,
  latest_note: null as
    | { body_snippet: string; author_username: string | null; created_at: string | null }
    | null,
}

it('lists cases, open and closed, from the Inbox itself', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/requests/')) return response(page([caseRow]))
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['records_officer'], counts, ['/inbox?view=cases&status=closed'])

  // A closed case is reachable here. The work queues cannot show it, because
  // "closed" is a legal status and they filter on work state.
  expect(await screen.findByText('Jane Q. Public')).toBeInTheDocument()
  const row = screen.getAllByRole('row')[1]
  expect(row).toHaveTextContent('PRR-2026-0042')
  expect(row).toHaveTextContent('Closed – Withdrawn')
  expect(row).toHaveTextContent('Stadium project emails')
  expect(screen.getByRole('link', { name: /^Open case PRR-2026-0042/ }))
    .toHaveAttribute('href', '/requests/PRR-2026-0042')
})

it('marks a subject the model proposed, and leaves a described one plain', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([
    caseRow,
    {
      ...caseRow,
      id: 8,
      number: 'PRR-2026-0043',
      subject: 'Bus camera footage, Route 12',
      subject_source: 'ai' as const,
    },
  ]))))

  renderInbox(['records_officer'], counts, ['/inbox?view=cases'])

  const advisory = (await screen.findByText('Bus camera footage, Route 12'))
    .closest('td')!
  // Advisory text is labelled as advisory. It is a scanning aid nobody
  // adopted, and the request itself is untouched.
  expect(advisory).toHaveTextContent('Suggested by AI, advisory only:')
  const described = screen.getByText('Stadium project emails').closest('td')!
  expect(described).not.toHaveTextContent('Suggested by AI')
})

it('sorts server-side, records the column in the URL, and resets the page', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([caseRow]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=cases&page=3'])

  const received = await screen.findByRole('button', { name: /Received/ })
  expect(received.closest('th')).toHaveAttribute('aria-sort', 'none')

  await user.click(received)
  await waitFor(() =>
    expect(urls.some((url) => url.includes('ordering=received'))).toBe(true))
  // Page three of the previous order is a different set of cases.
  expect(urls.at(-1)).not.toContain('page=3')
  expect(screen.getByRole('button', { name: /Received/ }).closest('th'))
    .toHaveAttribute('aria-sort', 'ascending')

  await user.click(screen.getByRole('button', { name: /Received/ }))
  await waitFor(() =>
    expect(urls.some((url) => url.includes('ordering=-received'))).toBe(true))
  expect(screen.getByRole('button', { name: /Received/ }).closest('th'))
    .toHaveAttribute('aria-sort', 'descending')

  // A third activation returns to the view's own default ordering, which is
  // the only order that answers "what is late".
  await user.click(screen.getByRole('button', { name: /Received/ }))
  await waitFor(() =>
    expect(urls.at(-1)).not.toContain('ordering='))
})

it('says when a column sort has replaced the view default, and restores it', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([caseRow]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=cases_open&sort=-assignee'])

  // The view description promises flagged-first ordering; under a column sort
  // that promise is false, so the page says what order is on screen instead.
  const notice = await screen.findByText(/Sorted by Assignee, descending/)
  expect(notice).toHaveTextContent('default order is off')
  expect(screen.queryByText(/new requester email first/)).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Restore default order' }))
  await waitFor(() => expect(urls.at(-1)).not.toContain('ordering='))
  expect(await screen.findByText(/new requester email first/)).toBeInTheDocument()
  expect(screen.queryByText(/Sorted by/)).not.toBeInTheDocument()
})

it('restores a sorted column from the URL', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([caseRow]))
  }))

  renderInbox(['records_officer'], counts, ['/inbox?view=cases&sort=-requester'])

  expect((await screen.findByRole('button', { name: /Requester/ })).closest('th'))
    .toHaveAttribute('aria-sort', 'descending')
  expect(urls.some((url) => url.includes('ordering=-requester'))).toBe(true)
})

it('ignores a sort key the server does not whitelist', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([caseRow]))
  }))

  renderInbox(['records_officer'], counts, ['/inbox?view=cases&sort=requester__email'])

  await screen.findByText('PRR-2026-0042')
  // A stale bookmark must not leave the header claiming an order the server
  // never applied.
  expect(urls.every((url) => !url.includes('ordering='))).toBe(true)
})

it('keeps the estimate to All cases so the triage view fits a laptop', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([caseRow]))))

  const { unmount } = renderInbox(['records_officer'], counts, ['/inbox?view=cases_open'])
  await screen.findByRole('columnheader', { name: /Assignee/ })
  expect(screen.queryByRole('columnheader', { name: /Estimated/ })).toBeNull()
  unmount()

  renderInbox(['records_officer'], counts, ['/inbox?view=cases'])
  expect(await screen.findByRole('columnheader', { name: /Estimated/ }))
    .toBeInTheDocument()
})

it('offers no requester column to a role that may not see requesters', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([{
    ...caseRow,
    requester_email: null,
    requester_name: null,
  }]))))

  renderInbox(['reviewer'], counts, ['/inbox?view=cases'])

  await screen.findByText('PRR-2026-0042')
  expect(screen.queryByRole('columnheader', { name: /Requester/ })).toBeNull()
  // Nor the batch selection, which is an officer action.
  expect(screen.queryByRole('checkbox')).toBeNull()
})

it('keeps the case status filter and search in the URL', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=cases'])

  await user.selectOptions(
    await screen.findByRole('combobox', { name: 'Case status' }),
    'denied',
  )
  await waitFor(() =>
    expect(urls.some((url) => url.includes('status=denied'))).toBe(true),
  )

  await user.type(screen.getByRole('searchbox', { name: 'Search cases' }), 'Harborview')
  await user.click(screen.getByRole('button', { name: 'Search' }))
  // Search is the only way to reach a case by number or requester, so it has
  // to survive a reload and a shared link.
  await waitFor(() =>
    expect(urls.some((url) => url.includes('search=Harborview'))).toBe(true),
  )
})

it('states the case total so a bounded page never reads as the whole list', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/requests/')) {
      // More cases than one page holds: the officer must be told.
      return response({
        count: 128, next: '?page=2', previous: null, results: [caseRow],
      })
    }
    throw new Error(`Unexpected fetch: ${url}`)
  }))

  renderInbox(['records_officer'], counts, ['/inbox?view=cases'])

  // A truncated case list that looks complete is how a case gets reported as
  // not existing.
  expect(await screen.findByText(/128/)).toBeInTheDocument()
})

it('gives a reviewer the case index and no cross-case message queues', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/requests/')) return response(page([caseRow]))
    throw new Error(`Unexpected fetch: ${url}`)
  }))

  renderInbox(['reviewer'], counts)

  // The server scopes which cases a reviewer sees and blanks requester
  // identity for them; the Inbox must not offer them a message queue at all.
  expect(await screen.findByRole('button', { name: /All cases/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Needs attention/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Awaiting statutory response/ }))
    .not.toBeInTheDocument()
})

it('gives a liaison every queue on the bar without a menu', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([]))))

  renderInbox(['liaison'], counts)

  expect(await screen.findByRole('button', { name: /Assigned to you/ }))
    .toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^Team/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^Critical/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /More views/ }))
    .not.toBeInTheDocument()
})

async function openMoreViews(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: /^More views/ }))
}

it('stores the selected Inbox view in browser history and restores it', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('work_state=needs_attention')) return response(page([]))
    if (url.includes('work_state=waiting')) return response(page([]))
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/inbox?view=needs_attention']}>
        <HistoryProbe />
        <Inbox roles={['records_officer']} />
      </MemoryRouter>
    </QueryClientProvider>,
  )

  await openMoreViews(user)
  await user.click(await screen.findByRole('button', { name: 'Replies waiting' }))
  expect(screen.getByLabelText('Current search')).toHaveTextContent('?view=waiting')
  await user.click(screen.getByRole('button', { name: 'Back' }))
  expect(await screen.findByRole('button', { name: 'Needs attention' }))
    .toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByLabelText('Current search'))
    .toHaveTextContent('?view=needs_attention')
})

it('loads the canonical quarantined inbound queue from a reloadable URL', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/staff/inbound-correspondence-receipts/?queue=quarantined') {
      return response(page([]))
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['records_officer'], undefined, ['/inbox?view=quarantined'])

  expect(await screen.findByRole('button', { name: 'More views: Held for review' }))
    .toHaveAttribute('aria-pressed', 'true')
  expect(await screen.findByText('No messages are waiting in this queue.'))
    .toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('loads the legacy received-message queue from its canonical Inbox URL', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/staff/correspondence/?status=received&work_kind=inbound_reply&work_kind=email_intake&case_state=open') {
      return response(page([inboxMessage]))
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['records_officer'], undefined, ['/inbox?view=received'])

  expect(await screen.findByRole('button', { name: 'More views: Received' }))
    .toHaveAttribute('aria-pressed', 'true')
  expect(await screen.findByRole('link', { name: /^Open case / }))
    .toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('gives an admin only the PII-free delivery-exception queue', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/staff/work-items/?queue=inbox_delivery_exceptions') {
      return response(page([exception]))
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['admin'])

  expect(await screen.findByText('Resolve delivery exception')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Needs attention' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Delivery exceptions' })).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('shows complete accessible names for each Inbox view count', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([inboxMessage]))))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts)

  expect(await screen.findByLabelText('1 item in Needs attention')).toBeInTheDocument()
  expect(screen.getByText('Needs attention')).toBeInTheDocument()
  // The Cases chip counts open cases, the thing its list shows, not the
  // flagged subset; Unread is unread messages still waiting.
  expect(screen.getByLabelText('4 items in Cases')).toBeInTheDocument()
  expect(screen.getByLabelText('1 item in Unread')).toBeInTheDocument()
  // A queue behind the menu keeps its spoken count; moving it out of the bar
  // must not turn its badge into a bare number.
  await openMoreViews(user)
  expect(screen.getByLabelText('1 item in Delivery exceptions')).toBeInTheDocument()
})

const statutoryWork = {
  id: 'work-statutory',
  source_type: 'request',
  source_id: 'request-1',
  work_kind: 'request_action',
  request_number: 'PRR-2026-0042',
  assignee_id: null,
  assignee_username: null,
  disposition: 'needs_attention',
  version: 1,
  is_read: false,
  read_at: null,
  urgency_date: '2026-08-31',
  next_action: 'Send the statutory response',
  detail_url: '/work/work-statutory',
  source_url: '/requests/PRR-2026-0042',
  disposed_at: null,
  created_at: '2026-08-23T10:00:00Z',
  updated_at: '2026-08-23T10:00:00Z',
}

it('keeps decide-now queues in the bar and reference queues behind the menu', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([inboxMessage]))))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts)

  // The queues where not looking has a consequence stay one click away.
  const bar = await screen.findByRole('group', { name: 'Inbox views' })
  expect(bar).toContainElement(
    screen.getByRole('button', { name: /Awaiting statutory response/ }),
  )
  expect(bar).toContainElement(screen.getByRole('button', { name: /Needs attention/ }))
  // Reference-scale queues are still reachable and still grouped exactly as
  // the approved taxonomy names them - only the container changed.
  expect(screen.queryByRole('button', { name: /^All messages/ })).not.toBeInTheDocument()
  await openMoreViews(user)
  const reference = screen.getByRole('group', { name: 'Inbox views: Reference' })
  expect(reference).toContainElement(screen.getByRole('button', { name: /^All messages/ }))
  expect(screen.getByRole('group', { name: 'Inbox views: In progress' }))
    .toBeInTheDocument()
})

it('lists the statutory backlog with its due date and deadline caveat', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/staff/work-items/?queue=inbox_awaiting_statutory_response') {
      return response(page([statutoryWork]))
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(
    ['records_officer'],
    counts,
    ['/inbox?view=awaiting_statutory_response'],
  )

  expect(await screen.findByText('Send the statutory response')).toBeInTheDocument()
  expect(screen.getByText(/Statutory response due: Aug 31, 2026/)).toBeInTheDocument()
  expect(screen.getByText(/does not satisfy the deadline/)).toBeInTheDocument()
  expect(screen.getByLabelText('2 items in Awaiting statutory response'))
    .toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('gives officers subject and snippet triage context on cross-case rows', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([{
    ...inboxMessage,
    subject: 'Re: bus camera footage request',
    snippet: 'Following up on the footage from the afternoon route.',
    attachment_count: 2,
  }]))))

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  expect(await screen.findByText('Re: bus camera footage request')).toBeInTheDocument()
  expect(
    screen.getByText('Following up on the footage from the afternoon route.'),
  ).toBeInTheDocument()
  // Tags are one small line of plain text at the bottom of the row now,
  // not four uppercase pills stacked above the message.
  expect(screen.getByRole('listitem')).toHaveTextContent('2 attachments')
})

it('never shows message content on the administrator delivery-exception view', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/staff/work-items/?queue=inbox_delivery_exceptions') {
      return response(page([exception]))
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['admin'], counts)

  expect(await screen.findByText('Resolve delivery exception')).toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: /Awaiting statutory response/ }),
  ).not.toBeInTheDocument()
  expect(screen.queryByText(/Statutory response due/)).not.toBeInTheDocument()
})

it('names the row by its subject and states where the case legally stands', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([emailIntakeMessage]))))

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  // "Review emailed request" on every row is what made fourteen items
  // unreadable; the subject is what tells them apart.
  expect(
    await screen.findByText('NAGARA webinar: records retention'),
  ).toBeInTheDocument()
  const row = screen.getByRole('listitem')
  expect(row).toHaveTextContent('Emailed request')
  expect(row).toHaveTextContent('Received')
  expect(row).toHaveTextContent('Five-day response not sent')
  expect(row).toHaveTextContent('PRR-2026-0014 · Response due Sep 4, 2026')
})

it('marks a message handled from the row without opening the case', async () => {
  const calls: Array<{ url: string; body: unknown }> = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      calls.push({ url, body: JSON.parse(String(init.body)) })
      return response({})
    }
    return response(page([inboxMessage]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  await user.click(await screen.findByRole('button', { name: /^Mark handled: / }))

  await waitFor(() => expect(calls).toHaveLength(1))
  expect(at(calls, 0).url).toBe('/api/staff/work-items/work-1/disposition/')
  expect(at(calls, 0).body).toMatchObject({
    disposition: 'handled',
    expected_version: 1,
  })
})

it('closes an auto-created email case as a non-request and clears its row', async () => {
  const calls: Array<{ url: string; body: unknown }> = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      calls.push({ url, body: JSON.parse(String(init.body)) })
      return response({})
    }
    return response(page([emailIntakeMessage]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  await user.click(
    await screen.findByRole('button', { name: /^Not a records request: / }),
  )
  await user.type(
    screen.getByRole('textbox', { name: /not a public records request/ }),
    'Vendor webinar invitation, no records were requested.',
  )
  await user.click(screen.getByRole('button', { name: 'Close as non-request' }))

  await waitFor(() => expect(calls).toHaveLength(2))
  expect(at(calls, 0).url).toBe('/api/staff/requests/PRR-2026-0014/close-non-request/')
  expect(at(calls, 0).body).toMatchObject({
    reason: 'Vendor webinar invitation, no records were requested.',
  })
  // Closing the case does not retire the receipt-backed work item, so the row
  // would otherwise stay in Needs attention forever.
  expect(at(calls, 1).url).toBe('/api/staff/work-items/work-2/disposition/')
  expect(at(calls, 1).body).toMatchObject({ disposition: 'handled' })
})

it('offers the non-request closure only where the API would accept it', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([inboxMessage]))))

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  expect(await screen.findByRole('button', { name: /^Mark handled: / }))
    .toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^Not a records request: / }))
    .not.toBeInTheDocument()
})

it('links the statutory backlog straight to a preset acknowledgment composer', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/work-items/?queue=inbox_awaiting_statutory_response') {
      return response(page([statutoryWork]))
    }
    throw new Error(`Unexpected fetch: ${String(input)}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(
    ['records_officer'],
    counts,
    ['/inbox?view=awaiting_statutory_response'],
  )

  expect(await screen.findByRole('link', { name: /^Prepare acknowledgment for / }))
    .toHaveAttribute(
      'href',
      '/requests/PRR-2026-0042/messages?compose=acknowledgment',
    )
})

it('carries the retired My Work queues as an Inbox chip group', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/work-items/?queue=personal') {
      return response(page([statutoryWork]))
    }
    throw new Error(`Unexpected fetch: ${String(input)}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['records_officer'], counts, ['/inbox?view=personal'])

  const user = userEvent.setup()
  expect(await screen.findByRole('button', { name: /Assigned to you/ }))
    .toHaveAttribute('aria-pressed', 'true')
  await openMoreViews(user)
  const queues = screen.getByRole('group', { name: 'Inbox views: Your queues' })
  expect(queues).toContainElement(screen.getByRole('button', { name: /^Team/ }))
  expect(await screen.findByText('Send the statutory response')).toBeInTheDocument()
})

it('gives a liaison the assignment queues and no cross-case message views', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/work-items/?queue=personal') {
      return response(page([]))
    }
    throw new Error(`Unexpected fetch: ${String(input)}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['liaison'], counts)

  expect(await screen.findByRole('button', { name: /Assigned to you/ }))
    .toHaveAttribute('aria-pressed', 'true')
  expect(screen.queryByRole('button', { name: 'Needs attention' }))
    .not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Delivery exceptions' }))
    .not.toBeInTheDocument()
})

const openWebCase = {
  ...caseRow,
  id: 48,
  number: 'PRR-2026-0048',
  status: 'received',
  closed_at: null,
  initial_response_delivered: false,
  inbox_needs_attention: 0,
  latest_inbound_at: null,
  next_due: '2026-08-08',
  next_due_basis: 'statutory' as const,
}

const openEmailedCase = {
  ...caseRow,
  id: 51,
  number: 'PRR-2026-0051',
  status: 'received',
  closed_at: null,
  requester_email: 'chloe@example.org',
  requester_name: 'Chloe Example',
  initial_response_delivered: false,
  inbox_needs_attention: 2,
  latest_inbound_at: '2026-09-08T17:52:00Z',
  next_due: '2026-09-15',
  next_due_basis: 'statutory' as const,
  latest_note: {
    body_snippet: 'Called the custodian; video pulled Tuesday.',
    author_username: 'officer',
    created_at: '2026-09-09T16:00:00Z',
  },
}

it('lands on open cases, one row each, and flags the case with waiting email', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    if (url === '/api/staff/requests/?queue=inbox' && method === 'GET') {
      return response(page([openEmailedCase, openWebCase]))
    }
    if (url === '/api/staff/requests/PRR-2026-0051/inbox-handled/' && method === 'POST') {
      return response({ handled: 2 })
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts)

  // A web-form case has no email row anywhere; it is only reachable from a
  // case-first Inbox, and it is the one owing a five-day response.
  await screen.findByText('PRR-2026-0051')
  const rows = screen.getAllByRole('row').slice(1)
  expect(rows).toHaveLength(2)
  expect(rows[0]).toHaveTextContent('PRR-2026-0051')
  // Unread mail is marked the way unread mail is marked: a dot and a bold
  // row, with the count available to a screen reader and on hover. It used
  // to be a "2 NEW" badge and a full-width button in a column of their own.
  expect(rows[0]).toHaveTextContent('2 new requester messages')
  expect(rows[0]).toHaveTextContent('Called the custodian; video pulled Tuesday.')
  expect(rows[1]).toHaveTextContent('PRR-2026-0048')
  expect(rows[1]).toHaveTextContent('Five-business-day statutory response, not yet sent')
  expect(rows[1]).not.toHaveTextContent('new requester message')
  expect(screen.queryByRole('columnheader', { name: /New activity/ }))
    .not.toBeInTheDocument()
  // The open-cases view is already one status band; only All cases filters.
  expect(screen.queryByRole('combobox', { name: 'Case status' })).not.toBeInTheDocument()

  // The action moved into the row's own menu.
  await user.click(
    screen.getByRole('button', { name: 'Actions for case PRR-2026-0051' }),
  )
  await user.click(await screen.findByRole('menuitem', { name: 'Mark caught up' }))

  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/requests/PRR-2026-0051/inbox-handled/',
      expect.objectContaining({ method: 'POST' }),
    ),
  )
})

it('opens a case when the row itself is clicked', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/requests/?queue=inbox') {
      return response(page([openWebCase]))
    }
    throw new Error(`Unexpected fetch: ${String(input)}`)
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts)

  const row = (await screen.findByText('PRR-2026-0048')).closest('tr')!
  // Not the link inside it: a case number is a small target to hit forty
  // times a day, so the whole row is the target.
  await user.click(row.querySelector('td:nth-child(4)')!)

  expect(await screen.findByTestId('location')).toHaveTextContent(
    '/requests/PRR-2026-0048',
  )
})

it('leaves the row alone when the click belongs to a control inside it', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/requests/?queue=inbox') {
      return response(page([openWebCase]))
    }
    throw new Error(`Unexpected fetch: ${String(input)}`)
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts)

  await screen.findByText('PRR-2026-0048')
  await user.click(
    screen.getByRole('checkbox', {
      name: 'Select case PRR-2026-0048 for a batch notice',
    }),
  )

  // Selecting a case for a batch must not also navigate away from the list
  // the batch is being built from.
  expect(screen.getByTestId('location')).toHaveTextContent('/inbox')
  expect(screen.getByText(/1 case selected/)).toBeInTheDocument()
})

it('marks every selected case caught up from the selection bar', async () => {
  const posted: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/staff/requests/?queue=inbox') {
      return response(page([openEmailedCase, openWebCase]))
    }
    if (url.endsWith('/inbox-handled/') && init?.method === 'POST') {
      posted.push(url)
      return response({ handled: 1 })
    }
    throw new Error(`Unexpected fetch: ${url}`)
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts)

  await screen.findByText('PRR-2026-0051')
  await user.click(
    screen.getByRole('checkbox', {
      name: 'Select every case on this page for a batch notice',
    }),
  )
  await user.click(screen.getByRole('button', { name: 'Mark caught up' }))

  // One call per case, so each keeps its own audit entry.
  await waitFor(() => expect(posted).toHaveLength(2))
  expect(posted).toEqual([
    '/api/staff/requests/PRR-2026-0051/inbox-handled/',
    '/api/staff/requests/PRR-2026-0048/inbox-handled/',
  ])
})


it('adds an auto-created email message to an existing case and clears its row', async () => {
  const calls: Array<{ url: string; body: unknown }> = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      calls.push({ url, body: JSON.parse(String(init.body)) })
      return response({})
    }
    if (url.startsWith('/api/staff/inbound-correspondence-receipts/receipt-1/')) {
      return response({ id: 'receipt-1', version: 7 })
    }
    return response(page([emailIntakeMessage]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  await user.click(
    await screen.findByRole('button', { name: /^Add to an existing case: / }),
  )
  await user.type(
    await screen.findByRole('textbox', { name: /Case to add this message to/ }),
    'PRR-2026-0042',
  )
  await user.type(
    screen.getByRole('textbox', { name: /Why this message belongs to that case/ }),
    'The requester is following up on their open stadium request.',
  )
  await waitFor(() => expect(
    screen.getByRole('button', { name: 'Add to case' }),
  ).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Add to case' }))

  await waitFor(() => expect(calls).toHaveLength(2))
  expect(at(calls, 0).url).toBe(
    '/api/staff/inbound-correspondence-receipts/receipt-1/redirect/',
  )
  expect(at(calls, 0).body).toMatchObject({
    request_number: 'PRR-2026-0042',
    reason: 'The requester is following up on their open stadium request.',
    // The version the server checks comes from the receipt, freshly read.
    expected_version: 7,
  })
  // Moving the message off this case leaves the receipt-backed work item
  // exactly where it was, so the finished row is retired in the same action.
  expect(at(calls, 1).url).toBe('/api/staff/work-items/work-2/disposition/')
  expect(at(calls, 1).body).toMatchObject({ disposition: 'handled' })
})

it('still reports the closure when only clearing the row fails', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/disposition/')) {
      return { ok: false, status: 409, text: async () => '{"detail":"stale"}' } as Response
    }
    if (init?.method === 'POST') return response({})
    return response(page([emailIntakeMessage]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  await user.click(
    await screen.findByRole('button', { name: /^Not a records request: / }),
  )
  await user.type(
    screen.getByRole('textbox', { name: /not a public records request/ }),
    'Vendor webinar invitation, no records were requested.',
  )
  await user.click(screen.getByRole('button', { name: 'Close as non-request' }))

  // The case really is closed, so the dialog must not sit there looking stuck
  // over work the server already recorded.
  await waitFor(() => expect(
    screen.queryByRole('heading', { name: 'Close as not a records request' }),
  ).not.toBeInTheDocument())
})

it('offers the move only on a case that was opened automatically', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([inboxMessage]))))

  renderInbox(['records_officer'], counts, ['/inbox?view=needs_attention'])

  expect(await screen.findByRole('button', { name: /^Mark handled: / }))
    .toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^Add to an existing case: / }))
    .not.toBeInTheDocument()
})

it('never batches a selected case the officer can no longer see', async () => {
  const otherCase = { ...caseRow, id: 8, number: 'PRR-2026-0043' }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/requests/')) {
      return response(page(url.includes('search=0043') ? [otherCase] : [caseRow]))
    }
    throw new Error(`Unexpected fetch: ${url}`)
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=cases'])

  await user.click(
    await screen.findByRole('checkbox', {
      name: 'Select case PRR-2026-0042 for a batch notice',
    }),
  )
  expect(screen.getByText('1 case selected.')).toBeInTheDocument()

  await user.type(screen.getByRole('searchbox', { name: 'Search cases' }), '0043')
  await user.click(screen.getByRole('button', { name: 'Search' }))

  // The selected case dropped out of the list. A batch is "the rows I can
  // see", so it must not ride along invisibly.
  await screen.findByRole('checkbox', {
    name: 'Select case PRR-2026-0043 for a batch notice',
  })
  expect(screen.queryByText(/selected\.$/)).not.toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: 'Draft notices for these cases' }),
  ).not.toBeInTheDocument()
})

it('drafts one notice for several selected cases before sending any of them', async () => {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      posts.push({ url, body: JSON.parse(String(init.body)) })
      if (url.endsWith('/batch/closure-drafts/')) {
        return response({
          batch_id: 'batch-1',
          succeeded: [
            {
              request_number: 'PRR-2026-0042',
              correspondence_id: 'draft-1',
              subject: 'PRR-2026-0042 — Your request is complete',
              body: 'Request PRR-2026-0042 is complete.\n',
            },
          ],
          failed: [],
        })
      }
      return response({ batch_id: 'batch-1', succeeded: [], failed: [] })
    }
    if (url.startsWith('/api/staff/requests/')) return response(page([caseRow]))
    throw new Error(`Unexpected fetch: ${url}`)
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=cases'])

  await user.click(
    await screen.findByRole('checkbox', {
      name: 'Select case PRR-2026-0042 for a batch notice',
    }),
  )
  await user.click(
    screen.getByRole('button', { name: 'Draft notices for these cases' }),
  )
  await user.type(
    screen.getByRole('textbox', { name: /What the District produced/ }),
    'The District produced every responsive record it located.',
  )
  await user.click(screen.getByRole('button', { name: 'Create drafts' }))

  // Drafting is its own step: the officer is told what exists and that
  // nothing has gone out, and has to confirm a second time to send.
  expect(await screen.findByText(/Nothing has been sent yet/))
    .toBeInTheDocument()
  expect(posts).toHaveLength(1)
  expect(at(posts, 0).url).toContain('/batch/closure-drafts/')

  // Every case's own notice is on screen, and the send stays locked until
  // the officer says they read them -- a count alone is not a review.
  expect(screen.getByText('PRR-2026-0042 — Your request is complete'))
    .toBeInTheDocument()
  expect(screen.getByText(/Request PRR-2026-0042 is complete\./))
    .toBeInTheDocument()
  const sendButton = screen.getByRole('button', { name: /^Send 1 notice/ })
  expect(sendButton).toBeDisabled()
  await user.click(
    screen.getByRole('checkbox', { name: /I have read each notice/ }),
  )
  expect(sendButton).toBeEnabled()

  await user.click(sendButton)

  await waitFor(() => expect(posts).toHaveLength(2))
  expect(at(posts, 1).url).toContain('/batch/approve-and-send/')
  expect(at(posts, 1).body).toEqual({
    correspondence_ids: ['draft-1'],
    confirmed: true,
  })
})

it('opens All cases on open cases and offers open and closed as groups', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/inbox?view=cases'])

  const select = await screen.findByRole('combobox', { name: 'Case status' })
  // Closed cases are out of the way unless someone asks for them.
  expect(select).toHaveValue('open')
  await waitFor(() => expect(urls).toContain('/api/staff/requests/?status=open'))

  await user.selectOptions(select, 'closed')
  await waitFor(() => expect(urls).toContain('/api/staff/requests/?status=closed'))

  await user.selectOptions(select, 'all')
  await waitFor(() => expect(urls).toContain('/api/staff/requests/'))
})

it('never narrows the open-cases view by a status left over from All cases', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([]))
  }))

  renderInbox(['records_officer'], counts, ['/inbox?view=cases_open&status=closed'])

  await waitFor(() => expect(urls).toContain('/api/staff/requests/?queue=inbox'))
  expect(urls.some((url) => url.includes('status='))).toBe(false)
})

it('shows messages on open cases by default and closed ones on request', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([]))
  }))
  const user = userEvent.setup()
  const base = '/api/staff/correspondence/?work_state=handled&work_kind=inbound_reply&work_kind=email_intake'

  renderInbox(['records_officer'], counts, ['/inbox?view=handled'])

  const select = await screen.findByRole('combobox', { name: 'Case state' })
  expect(select).toHaveValue('open')
  await waitFor(() => expect(urls).toContain(`${base}&case_state=open`))

  await user.selectOptions(select, 'closed')
  await waitFor(() => expect(urls).toContain(`${base}&case_state=closed`))

  await user.selectOptions(select, 'all')
  await waitFor(() => expect(urls).toContain(base))
})

it('shows an unanswered intake label on the case row and opens its action', async () => {
  // #33: the officer can act on the suggested label from the Inbox.
  const suggested = {
    ...openEmailedCase,
    intake_suggestion: {
      label: 'duplicate_of', target_case_number: 'PRR-2026-0048', confidence: 0.9,
    },
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    if (url === '/api/staff/requests/?queue=inbox' && method === 'GET') {
      return response(page([suggested, openWebCase]))
    }
    if (url === '/api/staff/requests/PRR-2026-0051/advisory-intake/feedback/' && method === 'POST') {
      return response({ outcome: 'confirmed' })
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderInbox(['records_officer'], counts)

  const rows = (await screen.findAllByRole('row')).slice(1)
  expect(rows[0]).toHaveTextContent('AI suggests Duplicate of PRR-2026-0048 · 90%')
  expect(rows[1]).not.toHaveTextContent('AI suggests')

  await user.click(screen.getByRole('button', { name: 'Correct: Duplicate of PRR-2026-0048 for PRR-2026-0051' }))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/requests/PRR-2026-0051/advisory-intake/feedback/',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ outcome: 'confirmed', final_label: 'duplicate_of', reason: '' }),
    }),
  ))
  await waitFor(() =>
    expect(screen.getByTestId('location')).toHaveTextContent('/requests/PRR-2026-0051/intake'))
})
