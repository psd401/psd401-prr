import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  MemoryRouter,
  Outlet,
  Route,
  Routes,
  useLocation,
} from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import { Requests } from './Requests'
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
  initialEntries = ['/requests'],
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>
        <Routes>
          <Route element={<Outlet context={queueCounts} />}>
            <Route
              path="*"
              element={<><PathProbe /><Requests roles={roles} /></>}
            />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}


afterEach(() => vi.unstubAllGlobals())


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

it('lists closed requests when asked for them', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/requests/')) return response(page([caseRow]))
    throw new Error(`Unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderInbox(['records_officer'], counts, ['/requests?show=closed'])

  // A closed case is reachable here. The work queues cannot show it, because
  // "closed" is a legal status and they filter on work state.
  expect(await screen.findByText('Jane Q. Public')).toBeInTheDocument()
  const row = screen.getAllByRole('row')[1]
  expect(row).toHaveTextContent('PRR-2026-0042')
  expect(row).toHaveTextContent('Closed – Withdrawn')
  expect(row).toHaveTextContent('Stadium project emails')
  expect(screen.getByRole('link', { name: /^Open case PRR-2026-0042/ }))
    .toHaveAttribute('href', '/requests/PRR-2026-0042')
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/requests/?status=closed',
    expect.anything(),
  )
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

  renderInbox(['records_officer'], counts, ['/requests?show=all'])

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

  renderInbox(['records_officer'], counts, ['/requests?show=all&page=3'])

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

  renderInbox(['records_officer'], counts, ['/requests?sort=-assignee'])

  // The view description promises flagged-first ordering; under a column sort
  // that promise is false, so the page says what order is on screen instead.
  await screen.findByText(/Sorted by Assignee, descending/)

  await user.click(screen.getByRole('button', { name: 'Back to soonest due first' }))
  await waitFor(() => expect(urls.at(-1)).not.toContain('ordering='))
  expect(screen.queryByText(/Sorted by/)).not.toBeInTheDocument()
})

it('restores a sorted column from the URL', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([caseRow]))
  }))

  renderInbox(['records_officer'], counts, ['/requests?show=all&sort=-requester'])

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

  renderInbox(['records_officer'], counts, ['/requests?show=all&sort=requester__email'])

  await screen.findByText('PRR-2026-0042')
  // A stale bookmark must not leave the header claiming an order the server
  // never applied.
  expect(urls.every((url) => !url.includes('ordering='))).toBe(true)
})

it('keeps the estimate to All cases so the triage view fits a laptop', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([caseRow]))))

  const { unmount } = renderInbox(['records_officer'], counts, ['/requests'])
  await screen.findByRole('columnheader', { name: /Assignee/ })
  expect(screen.queryByRole('columnheader', { name: /Estimated/ })).toBeNull()
  unmount()

  renderInbox(['records_officer'], counts, ['/requests?show=all'])
  expect(await screen.findByRole('columnheader', { name: /Estimated/ }))
    .toBeInTheDocument()
})

it('offers no requester column to a role that may not see requesters', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(page([{
    ...caseRow,
    requester_email: null,
    requester_name: null,
  }]))))

  renderInbox(['reviewer'], counts, ['/requests?show=all'])

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

  renderInbox(['records_officer'], counts, ['/requests?show=all'])

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

  renderInbox(['records_officer'], counts, ['/requests?show=all'])

  // A truncated case list that looks complete is how a case gets reported as
  // not existing.
  expect(await screen.findByText(/128/)).toBeInTheDocument()
})

it('gives a reviewer the request list without officer-only filters', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/requests/')) return response(page([caseRow]))
    throw new Error(`Unexpected fetch: ${url}`)
  }))

  renderInbox(['reviewer'], counts)

  // The server scopes which cases a reviewer sees and blanks requester
  // identity for them; the Inbox must not offer them a message queue at all.
  expect(await screen.findByRole('button', { name: /^All/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Assigned to me/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /New email/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /5-day response due/ }))
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
  // Open requests by default; closed is one choice away.
  expect(screen.getByRole('combobox', { name: 'Case status' })).toHaveValue('open')

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
  expect(screen.getByTestId('location')).toHaveTextContent('/requests')
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

  renderInbox(['records_officer'], counts, ['/requests?show=all'])

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

it('denies and closes bot requests in one click, with no citation to pick', async () => {
  const posts: { url: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      posts.push({ url, body: JSON.parse(String(init.body)) })
      if (url.endsWith('/batch/closure-drafts/')) {
        return response({
          batch_id: 'batch-1',
          succeeded: [{
            request_number: 'PRR-2026-0042',
            correspondence_id: 'draft-1',
            subject: 'Public Records Request PRR-2026-0042 — Request Closed',
            body: 'Denied pursuant to RCW 42.56.080(3).\n',
          }],
          failed: [],
        })
      }
      return response({
        batch_id: 'batch-2',
        succeeded: [{
          request_number: 'PRR-2026-0042',
          correspondence_id: 'draft-1',
          subject: 's',
          body: 'b',
        }],
        failed: [],
      })
    }
    if (url.endsWith('/automatic-message-templates/')) {
      return response({
        templates: [
          { kind: 'general', names: ['Fee reminder'] },
          { kind: 'denial', names: ['', 'Denial – bot generated'] },
        ],
      })
    }
    if (url.startsWith('/api/staff/requests/')) return response(page([caseRow]))
    throw new Error(`Unexpected fetch: ${url}`)
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/requests?show=all'])

  await user.click(
    await screen.findByRole('checkbox', {
      name: 'Select case PRR-2026-0042 for a batch notice',
    }),
  )
  await user.click(screen.getByRole('button', { name: 'Deny as bot requests' }))
  const template = await screen.findByRole('combobox', { name: /^Denial template/ })
  await waitFor(() => expect(template).toHaveValue('Denial – bot generated'))
  expect(screen.queryByText(/exemption citation/i)).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Deny and close 1 case' }))

  expect(await screen.findByText(/Sent 1 bot-request denial/)).toBeInTheDocument()
  expect(posts.map((post) => post.url)).toEqual([
    '/api/staff/correspondence/batch/closure-drafts/',
    '/api/staff/correspondence/batch/approve-and-send/',
  ])
  expect(at(posts, 0).body).toMatchObject({
    request_numbers: ['PRR-2026-0042'],
    kind: 'denial',
    template_name: 'Denial – bot generated',
  })
  expect(at(posts, 1).body).toEqual({
    correspondence_ids: ['draft-1'],
    confirmed: true,
  })
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

  renderInbox(['records_officer'], counts, ['/requests?show=all'])

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

it('opens on open requests and offers closed and every status', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/requests'])

  const select = await screen.findByRole('combobox', { name: 'Case status' })
  // Closed requests are out of the way unless someone asks for them.
  expect(select).toHaveValue('open')
  await waitFor(() => expect(urls).toContain('/api/staff/requests/?queue=inbox'))

  await user.selectOptions(select, 'closed')
  await waitFor(() => expect(urls).toContain('/api/staff/requests/?status=closed'))

  await user.selectOptions(select, 'all')
  await waitFor(() => expect(urls).toContain('/api/staff/requests/'))
})

it('narrows open requests by what needs doing, with counts that match', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    return response(page([]))
  }))
  const user = userEvent.setup()

  renderInbox(['records_officer'], counts, ['/requests'])

  const due = await screen.findByRole('button', { name: /^5-day response due/ })
  expect(due).toHaveTextContent(String(counts.inbox_awaiting_statutory_response))
  await user.click(due)
  await waitFor(() =>
    expect(urls).toContain('/api/staff/requests/?queue=inbox&focus=response_due'))

  await user.click(screen.getByRole('button', { name: /^New email/ }))
  await waitFor(() =>
    expect(urls).toContain('/api/staff/requests/?queue=inbox&focus=new_email'))
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
