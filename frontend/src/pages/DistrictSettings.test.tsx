// ABOUTME: The Settings screen states each setting's consequence, refuses a
// ABOUTME: change without a reason, and sends the reason with the new value.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { DistrictSettings } from './DistrictSettings'
import { at } from '../test/at'

afterEach(() => vi.unstubAllGlobals())

const SAFETY_NET_CONSEQUENCE =
  'On, a request with no human response by its last RCW 42.56.520 business '
  + 'day gets the acknowledgment queued automatically. Off, there is no '
  + 'automatic day-5 acknowledgment, and a late acknowledgment is strict '
  + 'liability under RCW 42.56.520.'

const CAMERA_CONSEQUENCE =
  'How long the district’s cameras are believed to keep footage. Shortening '
  + 'it below the true retention means footage that still exists is never '
  + 'held and can be overwritten.'

const SECRET_CONSEQUENCE =
  'The secret paired with the sign-in client ID. It is write-only: it can be '
  + 'replaced or cleared, never read back. A wrong value fails every sign-in '
  + 'at the token exchange.'

/** The fields every row carries, whatever its type. */
const SHAPE = {
  unit: '',
  minimum: null,
  maximum: null,
  periods: [],
  max_length: null,
  max_items: null,
  allow_blank: false,
  choices: [],
  verification: null,
}

function settingsPayload(overrides: Record<string, unknown> = {}) {
  return {
    categories: [
      'Statutory compliance',
      'Gmail transport',
      'Records handling',
      'Domains and people',
      'Access and tokens',
      'Advisory AI',
      'Staff sign-in',
      'Google identities',
    ],
    reason_min_length: 10,
    results: [
      {
        key: 'AUTO_ACK_SAFETY_NET',
        label: 'Day-5 acknowledgment safety net',
        type: 'boolean',
        category: 'Statutory compliance',
        consequence: SAFETY_NET_CONSEQUENCE,
        value: true,
        default: false,
        ...SHAPE,
        requires: [],
        required_by: [],
        last_change: {
          id: 7,
          at: '2026-09-23T17:00:00Z',
          key: 'AUTO_ACK_SAFETY_NET',
          old_value: false,
          new_value: true,
          reason: 'Counsel asked for the statutory backstop before cutover.',
          source: 'admin',
          actor_email: 'admin@psd401.net',
          actor_name: 'Avery Admin',
        },
        ...overrides,
      },
      {
        key: 'PRR_GMAIL_ENABLED',
        label: 'Gmail transport',
        type: 'boolean',
        category: 'Gmail transport',
        consequence: 'On, the application may reach the pinned district mailbox.',
        value: false,
        default: false,
        ...SHAPE,
        requires: [],
        required_by: ['PRR_GMAIL_INBOUND_POLL_ENABLED'],
        last_change: null,
      },
      {
        key: 'CAMERA_RETENTION_DAYS',
        label: 'Camera retention window',
        type: 'integer',
        category: 'Records handling',
        consequence: CAMERA_CONSEQUENCE,
        value: 10,
        default: 10,
        ...SHAPE,
        unit: 'days',
        minimum: 1,
        maximum: 3650,
        requires: [],
        required_by: [],
        last_change: null,
      },
      {
        key: 'PRR_RECORD_PREVIEW_MAX_BYTES',
        label: 'Preview size limit',
        type: 'integer',
        category: 'Records handling',
        consequence: 'The largest record the reviewer preview will render.',
        value: 524288000,
        default: 524288000,
        ...SHAPE,
        unit: 'bytes',
        minimum: 1048576,
        maximum: 10737418240,
        requires: [],
        required_by: [],
        last_change: null,
      },
      {
        key: 'PRR_DISTRICT_EMAIL_DOMAINS',
        label: 'District mail domains',
        type: 'domain_list',
        category: 'Domains and people',
        consequence: 'A record whose sender is outside these domains is tagged.',
        value: ['psd401.net'],
        default: ['psd401.net'],
        ...SHAPE,
        max_items: 50,
        allow_blank: true,
        requires: [],
        required_by: [],
        last_change: null,
      },
      {
        key: 'PRR_STAFF_LOGIN_RATE',
        label: 'Staff login rate',
        type: 'rate',
        category: 'Access and tokens',
        consequence: 'How often a staff sign-in may be attempted from one address.',
        value: '10/min',
        default: '10/min',
        ...SHAPE,
        periods: ['s', 'sec', 'm', 'min', 'h', 'hour'],
        requires: [],
        required_by: [],
        last_change: null,
      },
      {
        key: 'PRR_AI_PROVIDER',
        label: 'Advisory model provider',
        type: 'text',
        category: 'Advisory AI',
        consequence: 'Which service the district has approved to see record text.',
        value: 'bedrock',
        default: 'bedrock',
        ...SHAPE,
        max_length: 7,
        requires: [],
        required_by: [],
        last_change: null,
      },
      {
        key: 'PRR_GOOGLE_OIDC_CLIENT_SECRET',
        label: 'Google sign-in client secret',
        type: 'secret',
        category: 'Staff sign-in',
        consequence: SECRET_CONSEQUENCE,
        value: {
          set: true,
          fingerprint: 'ab12cd34ef56',
          set_by: 'Robin Clerk',
          set_at: '2026-09-23T18:00:00Z',
        },
        default: null,
        ...SHAPE,
        max_length: 255,
        allow_blank: true,
        requires: [],
        required_by: [],
        last_change: null,
      },
      {
        key: 'PRR_AUTH_MODE',
        label: 'Staff sign-in mode',
        type: 'choice',
        category: 'Staff sign-in',
        consequence: 'How staff sign in: Google accounts, or local passwords.',
        value: 'google',
        default: 'google',
        ...SHAPE,
        choices: ['google', 'local'],
        requires: [],
        required_by: [],
        last_change: null,
      },
      {
        key: 'PRR_VAULT_IMPERSONATION_TARGET',
        label: 'Vault service identity',
        type: 'email',
        category: 'Google identities',
        consequence: 'The service account the application impersonates for Vault.',
        value: 'prr-vault@example.iam.gserviceaccount.com',
        default: '',
        ...SHAPE,
        max_length: 255,
        allow_blank: true,
        verification: {
          ok: false,
          detail: 'Google Vault credentials could not be resolved.',
          checked_at: '2026-09-24T09:00:00Z',
        },
        requires: [],
        required_by: [],
        last_change: null,
      },
    ],
  }
}

function ok(payload: unknown) {
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) }
}

/** Typed like the real fetch so `mock.calls` keeps its (url, init) shape. */
function stubFetch(
  handler: (request: RequestInfo | URL, init?: RequestInit) => Promise<unknown>,
) {
  const mock = vi.fn(handler)
  vi.stubGlobal('fetch', mock)
  return mock
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider><DistrictSettings /></ToastProvider>
    </QueryClientProvider>,
  )
}

it('states each switch consequence, its value, and who last changed it', async () => {
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  expect(await screen.findByText(SAFETY_NET_CONSEQUENCE)).toBeInTheDocument()
  expect(screen.getByText(/Avery Admin/)).toBeInTheDocument()
  expect(
    screen.getByText(/Counsel asked for the statutory backstop/),
  ).toBeInTheDocument()
  expect(
    screen.getAllByText('Never changed in the application.').length,
  ).toBeGreaterThan(0)
  expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Turn on' })).toBeInTheDocument()
})

it('refuses to send a change until a substantive reason is written', async () => {
  const user = userEvent.setup()
  const fetchMock = stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(await screen.findByRole('button', { name: 'Turn off' }))
  const dialog = await screen.findByRole('dialog')
  const submit = within(dialog).getByRole('button', { name: 'Turn off' })
  expect(submit).toBeDisabled()

  await user.type(within(dialog).getByRole('textbox'), 'too short')
  expect(submit).toBeDisabled()
  expect(
    fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH'),
  ).toBe(false)
})

it('sends the new value with the reason the admin wrote', async () => {
  const user = userEvent.setup()
  const fetchMock = stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(await screen.findByRole('button', { name: 'Turn off' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText(SAFETY_NET_CONSEQUENCE)).toBeInTheDocument()
  await user.type(
    within(dialog).getByRole('textbox'),
    'Pausing the safety net during the counsel-reviewed cutover window.',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Turn off' }))

  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patch).toBeTruthy()
    expect(String(patch![0])).toBe('/api/staff/settings/AUTO_ACK_SAFETY_NET/')
    expect(JSON.parse(String(patch![1]!.body))).toEqual({
      value: false,
      reason: 'Pausing the safety net during the counsel-reviewed cutover window.',
    })
  })
})

it('shows each non-switch setting’s current value and its bounds', async () => {
  const user = userEvent.setup()
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  // A limit reads as a quantity, not a bare number, and a byte ceiling reads
  // in the unit it was written in rather than as nine digits.
  expect(await screen.findByText('10 days')).toBeInTheDocument()
  expect(screen.getByText('500 MiB')).toBeInTheDocument()
  expect(screen.getByText('psd401.net')).toBeInTheDocument()
  expect(screen.getByText('10/min')).toBeInTheDocument()

  const changeButtons = screen.getAllByRole('button', { name: 'Change' })
  await user.click(at(changeButtons, 0))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText(CAMERA_CONSEQUENCE)).toBeInTheDocument()
  expect(within(dialog).getByText('Between 1 and 3,650 days.')).toBeInTheDocument()
  expect(within(dialog).getByRole('spinbutton')).toHaveValue(10)
})

it('sends a changed number with its reason', async () => {
  const user = userEvent.setup()
  const fetchMock = stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(at(await screen.findAllByRole('button', { name: 'Change' }), 0))
  const dialog = await screen.findByRole('dialog')
  const field = within(dialog).getByRole('spinbutton')
  await user.clear(field)
  await user.type(field, '45')
  await user.type(
    within(dialog).getByRole('textbox'),
    'Facilities confirmed the bus-video vendor keeps 45 days.',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Save change' }))

  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patch).toBeTruthy()
    expect(String(patch![0])).toBe('/api/staff/settings/CAMERA_RETENTION_DAYS/')
    expect(JSON.parse(String(patch![1]!.body))).toEqual({
      value: 45,
      reason: 'Facilities confirmed the bus-video vendor keeps 45 days.',
    })
  })
})

it('sends a domain list as a list, not as the text that was typed', async () => {
  const user = userEvent.setup()
  const fetchMock = stubFetch(async () => ok(settingsPayload()))

  renderPage()

  const buttons = await screen.findAllByRole('button', { name: 'Change' })
  await user.click(at(buttons, 2))
  const dialog = await screen.findByRole('dialog')
  const textboxes = within(dialog).getAllByRole('textbox')
  const field = at(textboxes, 0)
  const reason = at(textboxes, 1)
  expect(field).toHaveValue('psd401.net')
  await user.clear(field)
  await user.type(field, 'psd401.net, psd401.org ,')
  await user.type(reason, 'Adding the district’s second mail domain.')
  await user.click(within(dialog).getByRole('button', { name: 'Save change' }))

  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patch).toBeTruthy()
    expect(JSON.parse(String(patch![1]!.body)).value)
      .toEqual(['psd401.net', 'psd401.org'])
  })
})

it('sends a changed rate as the text that was typed, trimmed', async () => {
  const user = userEvent.setup()
  const fetchMock = stubFetch(async () => ok(settingsPayload()))

  renderPage()

  const buttons = await screen.findAllByRole('button', { name: 'Change' })
  await user.click(at(buttons, 3))
  const dialog = await screen.findByRole('dialog')
  expect(
    within(dialog).getByText(
      'A rate such as 10/hour, using one of: s, sec, m, min, h, hour.',
    ),
  ).toBeInTheDocument()
  const textboxes = within(dialog).getAllByRole('textbox')
  const field = at(textboxes, 0)
  const reason = at(textboxes, 1)
  expect(field).toHaveValue('10/min')
  await user.clear(field)
  await user.type(field, ' 20/hour ')
  await user.type(reason, 'Slowing sign-in attempts after the audit finding.')
  await user.click(within(dialog).getByRole('button', { name: 'Save change' }))

  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patch).toBeTruthy()
    expect(String(patch![0])).toBe('/api/staff/settings/PRR_STAFF_LOGIN_RATE/')
    expect(JSON.parse(String(patch![1]!.body)).value).toBe('20/hour')
  })
})

it('will not submit a number field left empty', async () => {
  const user = userEvent.setup()
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(at(await screen.findAllByRole('button', { name: 'Change' }), 0))
  const dialog = await screen.findByRole('dialog')
  await user.clear(within(dialog).getByRole('spinbutton'))
  await user.type(
    within(dialog).getByRole('textbox'),
    'Trying to clear the retention window entirely.',
  )
  expect(within(dialog).getByRole('alert')).toHaveTextContent(
    'Enter a whole number.',
  )
  expect(
    within(dialog).getByRole('button', { name: 'Save change' }),
  ).toBeDisabled()
})

it('refuses a number outside its bounds and names the limit', async () => {
  // The bounds came from the server with the row, so the page may act on
  // them; saying the limit itself is what makes the refusal readable instead
  // of a transient browser bubble.
  const user = userEvent.setup()
  const fetchMock = stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(at(await screen.findAllByRole('button', { name: 'Change' }), 0))
  const dialog = await screen.findByRole('dialog')
  const field = within(dialog).getByRole('spinbutton')
  await user.clear(field)
  await user.type(field, '5000')
  await user.type(
    within(dialog).getByRole('textbox'),
    'Attempting a retention window the district cannot have.',
  )
  expect(within(dialog).getByRole('alert')).toHaveTextContent(
    'Between 1 and 3,650 days.',
  )
  expect(
    within(dialog).getByRole('button', { name: 'Save change' }),
  ).toBeDisabled()
  expect(
    fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH'),
  ).toBe(false)
})

it('states a byte limit’s bounds in the unit the row is shown in', async () => {
  const user = userEvent.setup()
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(at(await screen.findAllByRole('button', { name: 'Change' }), 1))
  const dialog = await screen.findByRole('dialog')
  expect(
    within(dialog).getByText('Between 1 MiB and 10 GiB, entered in bytes.'),
  ).toBeInTheDocument()
  expect(within(dialog).getByRole('spinbutton')).toHaveValue(524288000)
})

it('clears a refused save once the admin edits the value', async () => {
  // Two alerts at once, one describing a value no longer on screen, is
  // noise a screen reader user cannot tell apart.
  const user = userEvent.setup()
  stubFetch(async (_request, init) => {
    if (init?.method === 'PATCH') {
      return {
        ok: false,
        status: 400,
        text: async () => '{"detail":"Refused by the settings store."}',
      }
    }
    return ok(settingsPayload())
  })

  renderPage()

  await user.click(at(await screen.findAllByRole('button', { name: 'Change' }), 0))
  const dialog = await screen.findByRole('dialog')
  const field = within(dialog).getByRole('spinbutton')
  await user.clear(field)
  await user.type(field, '45')
  await user.type(
    within(dialog).getByRole('textbox'),
    'Facilities confirmed the bus-video vendor keeps 45 days.',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Save change' }))
  expect(
    await within(dialog).findByText('Refused by the settings store.'),
  ).toBeInTheDocument()

  await user.type(field, '0')
  expect(
    within(dialog).queryByText('Refused by the settings store.'),
  ).not.toBeInTheDocument()
})

it('describes an installed secret without ever showing its value', async () => {
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  expect(await screen.findByText(SECRET_CONSEQUENCE)).toBeInTheDocument()
  expect(
    screen.getByText(/Installed, fingerprint ab12cd34ef56/),
  ).toBeInTheDocument()
  expect(screen.getByText(/cannot be read back/)).toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'Replace' }),
  ).toBeInTheDocument()
})

it('opens a secret editor that is empty, masked, and not pre-filled', async () => {
  const user = userEvent.setup()
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(await screen.findByRole('button', { name: 'Replace' }))
  const dialog = await screen.findByRole('dialog')

  expect(
    within(dialog).getByText('Replace Google sign-in client secret'),
  ).toBeInTheDocument()
  // A password input has no accessible textbox role, which is the point:
  // nothing on this page renders the value, not even as dots it could read
  // back from.
  const field = dialog.querySelector('input[type="password"]')
  expect(field).toBeTruthy()
  expect(field).toHaveValue('')
})

it('sends a replaced secret with its reason and keeps it out of the page', async () => {
  const user = userEvent.setup()
  const fetchMock = stubFetch(async () => ok(settingsPayload()))

  renderPage()

  await user.click(await screen.findByRole('button', { name: 'Replace' }))
  const dialog = await screen.findByRole('dialog')
  await user.type(
    dialog.querySelector('input[type="password"]')!,
    'GOCSPX-rotated-value',
  )
  await user.type(
    within(dialog).getByRole('textbox'),
    'Rotated the sign-in client secret with Google after the access review.',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Save change' }))

  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patch).toBeTruthy()
    expect(String(patch![0])).toBe(
      '/api/staff/settings/PRR_GOOGLE_OIDC_CLIENT_SECRET/',
    )
    expect(JSON.parse(String(patch![1]!.body))).toEqual({
      value: 'GOCSPX-rotated-value',
      reason:
        'Rotated the sign-in client secret with Google after the access review.',
    })
  })
  expect(document.body.textContent).not.toContain('GOCSPX-rotated-value')
})

it('says when a credential saved but did not authenticate', async () => {
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  expect(await screen.findByText('Unverified')).toBeInTheDocument()
  expect(
    screen.getByText(/did not authenticate when it was last checked/),
  ).toBeInTheDocument()
  expect(
    screen.getByText(/Google Vault credentials could not be resolved\./),
  ).toBeInTheDocument()
})

it('offers a sign-in mode as its choices rather than as free text', async () => {
  const user = userEvent.setup()
  stubFetch(async () => ok(settingsPayload()))

  renderPage()

  const row = (await screen.findByText('Staff sign-in mode')).closest('li')!
  await user.click(within(row).getByRole('button', { name: 'Change' }))
  const dialog = await screen.findByRole('dialog')

  const select = within(dialog).getByRole('combobox')
  expect(select).toHaveValue('google')
  expect(
    within(select).getAllByRole('option').map((option) => option.textContent),
  ).toEqual(['google', 'local'])
})

it('loads a setting history only when the admin asks for it', async () => {
  const user = userEvent.setup()
  const fetchMock = stubFetch(async (request: RequestInfo | URL) => {
    const path = String(request)
    if (path === '/api/staff/settings/AUTO_ACK_SAFETY_NET/history/') {
      return ok({
        key: 'AUTO_ACK_SAFETY_NET',
        results: [
          {
            id: 7,
            at: '2026-09-23T17:00:00Z',
            key: 'AUTO_ACK_SAFETY_NET',
            old_value: false,
            new_value: true,
            reason: 'Counsel asked for the statutory backstop before cutover.',
            source: 'admin',
            actor_email: 'admin@psd401.net',
            actor_name: 'Avery Admin',
          },
          {
            id: 1,
            at: '2026-08-01T17:00:00Z',
            key: 'AUTO_ACK_SAFETY_NET',
            old_value: null,
            new_value: false,
            reason: 'System import of the deployed value.',
            source: 'system_import',
            actor_email: '',
            actor_name: '',
          },
        ],
      })
    }
    return ok(settingsPayload())
  })

  renderPage()

  const historyButtons = await screen.findAllByRole('button', { name: 'History' })
  expect(
    fetchMock.mock.calls.some(([request]) => String(request).endsWith('/history/')),
  ).toBe(false)

  await user.click(at(historyButtons, 0))

  const imported = await screen.findByText('System import of the deployed value.')
  expect(imported.closest('li')).toHaveTextContent('by System import on')
  expect(
    screen.getByText('Counsel asked for the statutory backstop before cutover.'),
  ).toBeInTheDocument()
})

it('edits closure days as a list of dates and sends them as a list', async () => {
  const user = userEvent.setup()
  const holidays = {
    key: 'DISTRICT_HOLIDAYS',
    label: 'District closure days',
    type: 'date_list',
    category: 'Statutory compliance',
    consequence: 'Days the district is closed beyond Washington’s legal holidays.',
    value: ['2026-06-22'],
    default: [],
    ...SHAPE,
    max_items: 100,
    allow_blank: true,
    requires: [],
    required_by: [],
    last_change: null,
  }
  const fetchMock = stubFetch(async () => ok({
    categories: ['Statutory compliance'],
    reason_min_length: 10,
    results: [holidays],
  }))

  renderPage()

  expect(await screen.findByText('2026-06-22')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Change' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText(/Comma-separated dates in the form YYYY-MM-DD/))
    .toBeInTheDocument()
  const textboxes = within(dialog).getAllByRole('textbox')
  const field = at(textboxes, 0)
  const reason = at(textboxes, 1)
  await user.clear(field)
  await user.type(field, '2026-06-22, 2026-12-24')
  await user.type(reason, 'Board adopted the 2026-27 closure calendar.')
  await user.click(within(dialog).getByRole('button', { name: 'Save change' }))

  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patch).toBeTruthy()
    expect(JSON.parse(String(patch![1]!.body)).value)
      .toEqual(['2026-06-22', '2026-12-24'])
  })
})
