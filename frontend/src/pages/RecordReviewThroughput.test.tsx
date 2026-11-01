// ABOUTME: The review queue must let a reviewer separate machine mail and
// ABOUTME: counsel mail, and decide a visible selection in one audited pass.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { RecordItems } from './RecordItems'
import { CounselDomains } from './CounselDomains'
import { at } from '../test/at'

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function item(index: number, overrides: Record<string, unknown> = {}) {
  return {
    id: `${index}`.padStart(8, '0') + '-2222-4222-8222-222222222222',
    request_number: 'PRR-2026-0042',
    source_system: 'google_vault_gmail',
    custodian: 'alice@psd401.net',
    custodians: ['alice@psd401.net'],
    original_filename: `Out of Office reply ${index}.pdf`,
    sha256: `${index}`.padStart(64, 'b'),
    review_state: 'unreviewed',
    reviewed_by_username: null,
    reviewed_at: null,
    redaction_count: 0,
    second_review_required: false,
    second_reviewed_by_username: null,
    second_reviewed_at: null,
    redacted_artifact_ready: false,
    withholding_exemption_citations: [],
    created_at: '2026-08-01T12:00:00Z',
    content_type: 'application/pdf',
    content_length: 1024,
    handling: 'preview',
    handling_label: 'Ready to review',
    handling_detail: 'Opens in the reviewer and supports redaction.',
    derivative_state: 'none',
    derivative_detail: '',
    reviewable: true,
    tags: ['auto_reply'],
    email_from: 'ann@vendor.example',
    email_to: 'alice@psd401.net',
    email_cc: '',
    email_sent_at: '2026-08-01T11:00:00Z',
    email_message_id: `<${index}@vendor.example>`,
    email_in_reply_to: '',
    email_sender_domain: 'vendor.example',
    email_auto_submitted: 'auto-replied',
    email_list_id: '',
    email_precedence: '',
    email_has_calendar_part: false,
    advisory_suggested_state: 'non_responsive',
    advisory_confidence: 0.82,
    ...overrides,
  }
}

const FACETS = {
  total: 3,
  privilege: 1,
  tags: [
    { value: 'auto_reply', label: 'Automatic reply', count: 3 },
    { value: 'calendar', label: 'Calendar', count: 0 },
    { value: 'bulk_list', label: 'Bulk or list mail', count: 0 },
    { value: 'external_sender', label: 'External sender', count: 3 },
    { value: 'counsel_domain', label: 'District counsel', count: 1 },
    { value: 'privilege_marker', label: 'Privilege marker', count: 0 },
  ],
  sender_domains: [{ value: 'vendor.example', count: 3 }],
  custodians: [{ value: 'alice@psd401.net', count: 3 }],
}

function listResponse(count = 3) {
  return {
    count,
    next: null,
    previous: null,
    results: Array.from({ length: count }, (_, index) => item(index)),
  }
}

function stubApi(extra?: (url: string) => Response | null) {
  const posts: { url: string; body: unknown }[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST' || init?.method === 'PATCH' || init?.method === 'DELETE') {
      posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null })
      if (url.includes('bulk-review')) {
        return response({
          reviewed_count: 3,
          review_state: 'non_responsive',
          record_item_ids: [],
        })
      }
      return response({ id: 1, domain: 'lawfirm.example', note: '', active: true })
    }
    const override = extra?.(url)
    if (override) return override
    if (url.startsWith('/api/staff/record-items/facets/')) return response(FACETS)
    if (url.startsWith('/api/staff/record-items/')) return response(listResponse())
    return response({}, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, posts }
}

function renderList(roles: Parameters<typeof RecordItems>[0]['roles'] = ['reviewer']) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/?request=PRR-2026-0042']}>
          <RecordItems roles={roles} requestNumber="PRR-2026-0042" />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

function renderCounselDomains() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter><CounselDomains /></MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('review queue filters', () => {
  it('shows each record’s deterministic tags and every custodian it was found in', async () => {
    stubApi()
    renderList()

    await screen.findAllByText('Automatic reply')
    expect(await screen.findAllByText('External sender')).not.toHaveLength(0)
    expect(screen.getAllByText('alice@psd401.net').length).toBeGreaterThan(0)
  })

  it('names every mailbox behind one deduplicated message', async () => {
    stubApi((url) => (
      url.startsWith('/api/staff/record-items/') && !url.includes('facets')
        ? response({
          count: 1,
          next: null,
          previous: null,
          results: [item(0, {
            custodians: ['alice@psd401.net', 'bob@psd401.net', 'carol@psd401.net'],
          })],
        })
        : null
    ))
    renderList()

    expect(await screen.findByText('+2 more')).toBeTruthy()
  })

  it('falls back to the sending domain when the API withholds the address', async () => {
    // Only a Records Officer is served the correspondent addresses. A
    // reviewer still needs to see who a message came from at the level that
    // is not identity, so the column reads the domain rather than going
    // blank.
    stubApi((url) => (
      url.startsWith('/api/staff/record-items/') && !url.includes('facets')
        ? response({
          count: 1,
          next: null,
          previous: null,
          results: [item(0, {
            email_from: '',
            email_to: '',
            email_cc: '',
            email_sender_domain: 'vendor.example',
          })],
        })
        : null
    ))
    renderList()

    expect(await screen.findAllByText('vendor.example')).not.toHaveLength(0)
    expect(screen.queryByText('ann@vendor.example')).toBeNull()
  })

  it('asks the server for the privilege queue rather than filtering one page', async () => {
    const { fetchMock } = stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findAllByText('Automatic reply')

    await user.selectOptions(screen.getByLabelText('Tag'), 'privilege')

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) => String(input).includes('tag=privilege')),
      ).toBe(true)
    })
  })

  it('asks the server to narrow by sender domain and custodian', async () => {
    const { fetchMock } = stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findAllByText('Automatic reply')

    await screen.findByRole('option', { name: /vendor\.example/ })
    await user.selectOptions(screen.getByLabelText('Sender domain'), 'vendor.example')
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes('sender_domain=vendor.example'),
        ),
      ).toBe(true)
    })

    await user.selectOptions(screen.getByLabelText('Custodian'), 'alice@psd401.net')
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes('custodian=alice%40psd401.net'),
        ),
      ).toBe(true)
    })
  })

  it('carries the AI confidence filter to the server without applying anything', async () => {
    const { fetchMock, posts } = stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findAllByText('Automatic reply')

    await user.selectOptions(screen.getByLabelText('AI confidence'), '0.9')

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes('confidence_min=0.9'),
        ),
      ).toBe(true)
    })
    expect(posts).toHaveLength(0)
  })

  it('groups the visible rows without asking for a different set', async () => {
    stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findAllByText('Automatic reply')

    await user.selectOptions(screen.getByLabelText('Group by'), 'sender_domain')

    expect(
      await screen.findByRole('heading', { name: /vendor\.example/ }),
    ).toBeTruthy()
  })
})

describe('record search', () => {
  it('promises a reviewer only what their search matches', async () => {
    stubApi()
    renderList()

    expect(
      await screen.findByPlaceholderText('Search filenames and extracted text'),
    ).toBeInTheDocument()
  })

  it('tells a records officer the search also covers correspondents', async () => {
    stubApi()
    renderList(['records_officer'])

    expect(
      await screen.findByPlaceholderText(
        'Search filenames, extracted text, and correspondents',
      ),
    ).toBeInTheDocument()
  })
})

describe('bulk human review', () => {
  it('leaves withheld and duplicate records out of a bulk selection', async () => {
    // Both are one-at-a-time decisions the service refuses in a batch, so
    // select-all must not stage them and their boxes cannot be ticked.
    const rows = [
      item(0),
      item(1, { review_state: 'withheld', original_filename: 'Withheld memo.pdf' }),
      item(2, { review_state: 'duplicate', original_filename: 'Second copy.pdf' }),
    ]
    const { posts } = stubApi((url) => (
      url.startsWith('/api/staff/record-items/') && !url.includes('/facets/')
        ? response({ count: 3, next: null, previous: null, results: rows })
        : null
    ))
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    expect(screen.getByLabelText('Select Withheld memo.pdf')).toBeDisabled()
    expect(screen.getByLabelText('Select Second copy.pdf')).toBeDisabled()
    await user.click(screen.getByLabelText('Select every record on this page'))
    expect(screen.getByLabelText('Select Out of Office reply 0.pdf')).toBeChecked()
    expect(screen.getByLabelText('Select Withheld memo.pdf')).not.toBeChecked()
    expect(screen.getByLabelText('Select Second copy.pdf')).not.toBeChecked()
    await user.selectOptions(screen.getByLabelText('Bulk review outcome'), 'non_responsive')
    await user.click(screen.getByRole('button', { name: 'Review selected' }))
    await user.click(screen.getByRole('button', { name: /Record 1 outcome/ }))

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(at(posts, 0).body).toEqual({
      record_item_ids: ['00000000-2222-4222-8222-222222222222'],
      state: 'non_responsive',
    })
  })

  it('records the selected records and the chosen outcome, and nothing else', async () => {
    const { posts } = stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    await user.selectOptions(screen.getByLabelText('Bulk review outcome'), 'non_responsive')
    await user.click(screen.getByRole('button', { name: 'Review selected' }))
    // The confirmation is deliberate: one click never writes a legal outcome.
    await user.click(screen.getByRole('button', { name: /Record 3 outcomes/ }))

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(at(posts, 0).url).toBe('/api/staff/record-items/bulk-review/')
    expect(at(posts, 0).body).toEqual({
      record_item_ids: [
        '00000000-2222-4222-8222-222222222222',
        '00000001-2222-4222-8222-222222222222',
        '00000002-2222-4222-8222-222222222222',
      ],
      state: 'non_responsive',
    })
  })

  it('says plainly that this is the reviewer’s decision, not an AI acceptance', async () => {
    stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    await user.selectOptions(screen.getByLabelText('Bulk review outcome'), 'responsive')
    await user.click(screen.getByRole('button', { name: 'Review selected' }))

    expect(
      await screen.findByText(/not an acceptance of any AI suggestion/i),
    ).toBeTruthy()
  })

  it('offers no withholding or duplicate outcome in bulk', async () => {
    stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    const outcomes = screen.getByLabelText('Bulk review outcome')

    expect(outcomes.textContent).not.toMatch(/Withheld/)
    expect(outcomes.textContent).not.toMatch(/Duplicate/)
  })

  it('drops the selection when the filter changes what is visible', async () => {
    stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    expect(screen.getByText('3 selected')).toBeTruthy()

    await user.selectOptions(screen.getByLabelText('Tag'), 'calendar')

    await waitFor(() => expect(screen.queryByText('3 selected')).toBeNull())
  })

  // The review-state chips narrow the list exactly as the other filters do, so
  // they have to drop the selection too. Carrying it would leave one
  // confirmation covering records the reviewer can no longer see -- which is
  // the one thing this control promises it never does.
  it('drops the selection when the review-state chips change what is visible', async () => {
    stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    expect(screen.getByText('3 selected')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Unreviewed' }))

    await waitFor(() => expect(screen.queryByText('3 selected')).toBeNull())
  })

  // The filters are not the only thing that changes the listed rows. A record
  // reviewed in the detail panel refetches the list in place and can drop out
  // of the active filter, without any filter having changed. A selected id
  // left behind would be submitted with the batch while being missing from
  // the privilege count the confirmation shows, so the selection is pruned to
  // whatever the list actually comes back with.
  it('drops a selected record that leaves the list without a filter change', async () => {
    let visible = 3
    stubApi((url) => (
      url.startsWith('/api/staff/record-items/facets/')
        ? null
        : url.startsWith('/api/staff/record-items/')
          ? response({
            count: visible,
            next: null,
            previous: null,
            results: Array.from({ length: visible }, (_, index) => item(index)),
          })
          : null
    ))
    const user = userEvent.setup()
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0 },
        mutations: { retry: false },
      },
    })
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <MemoryRouter initialEntries={['/?request=PRR-2026-0042']}>
            <RecordItems roles={['reviewer']} requestNumber="PRR-2026-0042" />
          </MemoryRouter>
        </ToastProvider>
      </QueryClientProvider>,
    )
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    expect(screen.getByText('3 selected')).toBeTruthy()

    // One record was decided individually and no longer matches the filter.
    // Every mutation on this page refreshes the list exactly this way.
    visible = 2
    await client.invalidateQueries({ queryKey: ['record-items'] })

    await waitFor(() => expect(screen.getByText('2 selected')).toBeTruthy())
    expect(screen.queryByText('3 selected')).toBeNull()
  })

  // Paging changes what is listed, so it changes what "selected" could mean.
  // A selection that survived it would put records off screen into a decision
  // the confirmation calls the reviewer's -- and the confirmation's privilege
  // count is read off the visible rows, so it could say "0 privileged" about
  // a batch that carried some.
  it('drops the selection when the reviewer pages', async () => {
    stubApi((url) => (
      url.startsWith('/api/staff/record-items/facets/')
        ? null
        : url.startsWith('/api/staff/record-items/')
          ? response({
            count: 120,
            next: null,
            previous: null,
            results: Array.from({ length: 3 }, (_, index) => item(index)),
          })
          : null
    ))
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    expect(screen.getByText('3 selected')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: /Next/ }))

    await waitFor(() => expect(screen.queryByText('3 selected')).toBeNull())
  })

  // Each group renders its own table with its own header checkbox. A
  // page-wide toggle there would stage records from groups the reviewer never
  // opened, for a decision the confirmation calls theirs.
  it('selects only the group whose header checkbox was clicked', async () => {
    stubApi((url) => (
      url.startsWith('/api/staff/record-items/facets/')
        ? null
        : url.startsWith('/api/staff/record-items/')
          ? response({
            count: 2,
            next: null,
            previous: null,
            results: [
              item(0, { tags: ['auto_reply'] }),
              item(1, {
                original_filename: 'Counsel memo.pdf',
                tags: ['counsel_domain'],
              }),
            ],
          })
          : null
    ))
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.selectOptions(screen.getByLabelText('Group by'), 'tag')
    const groupToggle = await screen.findByLabelText(
      'Select every record in District counsel',
    )
    await user.click(groupToggle)

    expect(screen.getByText('1 selected')).toBeTruthy()
    expect(
      screen.getByLabelText('Select Counsel memo.pdf'),
    ).toBeChecked()
    expect(
      screen.getByLabelText('Select Out of Office reply 0.pdf'),
    ).not.toBeChecked()
  })

  // The privilege queue exists so a human reads counsel and privilege-marked
  // records before anything ships. Bulk review blocks nothing -- counsel mail
  // is not automatically exempt and only a named reviewer with a citation may
  // withhold -- but a reviewer must not decide over one without being told.
  it('says how many selected records carry a privilege marker', async () => {
    stubApi((url) => (
      url.startsWith('/api/staff/record-items/facets/')
        ? null
        : url.startsWith('/api/staff/record-items/')
          ? response({
            count: 2,
            next: null,
            previous: null,
            results: [
              item(0),
              item(1, {
                original_filename: 'Counsel memo.pdf',
                tags: ['external_sender', 'counsel_domain'],
              }),
            ],
          })
          : null
    ))
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    await user.selectOptions(
      screen.getByLabelText('Bulk review outcome'), 'non_responsive',
    )
    await user.click(screen.getByRole('button', { name: 'Review selected' }))

    expect(
      screen.getByText(/1 record carries a district-counsel or privilege marker/),
    ).toBeTruthy()
  })

  it('says nothing about privilege when no selected record carries one', async () => {
    stubApi()
    const user = userEvent.setup()
    renderList()
    await screen.findByLabelText('Select every record on this page')

    await user.click(screen.getByLabelText('Select every record on this page'))
    await user.selectOptions(
      screen.getByLabelText('Bulk review outcome'), 'non_responsive',
    )
    await user.click(screen.getByRole('button', { name: 'Review selected' }))

    expect(screen.queryByText(/privilege marker\./)).toBeNull()
  })

  it('gives a role without review authority no bulk control', async () => {
    stubApi()
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <MemoryRouter>
            <RecordItems roles={['admin']} requestNumber="PRR-2026-0042" />
          </MemoryRouter>
        </ToastProvider>
      </QueryClientProvider>,
    )
    await screen.findAllByText('alice@psd401.net')

    expect(screen.queryByLabelText('Select every record on this page')).toBeNull()
  })
})

describe('district counsel domains', () => {
  it('starts empty and says so, because privilege attaches to the district’s own firms', async () => {
    stubApi((url) => (
      url.startsWith('/api/staff/counsel-domains/')
        ? response({ count: 0, next: null, previous: null, results: [] })
        : null
    ))
    renderCounselDomains()

    expect(
      await screen.findByText(/No counsel domains are configured/),
    ).toBeTruthy()
  })

  it('adds one domain through the administrator endpoint', async () => {
    const { posts } = stubApi((url) => (
      url.startsWith('/api/staff/counsel-domains/')
        ? response({ count: 0, next: null, previous: null, results: [] })
        : null
    ))
    const user = userEvent.setup()
    renderCounselDomains()
    await screen.findByText(/No counsel domains are configured/)

    await user.click(screen.getByRole('button', { name: 'Add domain' }))
    await user.type(screen.getByLabelText(/^Domain/), 'lawfirm.example')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(at(posts, 0).url).toBe('/api/staff/counsel-domains/')
    expect(at(posts, 0).body).toMatchObject({ domain: 'lawfirm.example', active: true })
  })
})
