// ABOUTME: Application admins can see the configurable Google-group role map
// ABOUTME: in the staff UI; the API remains the enforcement boundary.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { IdentitySettings } from './IdentitySettings'
import { at } from '../test/at'

afterEach(() => vi.unstubAllGlobals())

it('renders the configured district group without assuming a prefix', async () => {
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL) => {
    const path = String(request)
    if (path === '/api/staff/direct-role-grants/') {
      return { ok: true, status: 200, text: async () => JSON.stringify([]) }
    }
    const payload = path === '/api/staff/google-role-mappings/'
      ? {
          count: 1,
          next: null,
          previous: null,
          results: [{
            id: 1,
            role: 'liaison',
            role_label: 'Department Liaison',
            google_group: 'our-own-prefix-records@psd401.net',
            active: true,
            created_at: '2026-07-22T00:00:00Z',
            updated_at: '2026-07-22T00:00:00Z',
          }],
        }
      : []
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify(payload),
    }
  }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={qc}>
      <ToastProvider><IdentitySettings /></ToastProvider>
    </QueryClientProvider>,
  )

  expect(await screen.findByText('our-own-prefix-records@psd401.net')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Add mapping' })).toBeInTheDocument()
})

it('lists identity status and forgets a rotated token after the admin dismisses it', async () => {
  const user = userEvent.setup()
  const rotatedKey = '0123456789abcdef0123456789abcdef0123456789'
  const fetchMock = vi.fn(async (
    request: RequestInfo | URL,
    options?: RequestInit,
  ) => {
    const path = String(request)
    if (path === '/api/staff/direct-role-grants/') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify([]),
      }
    }
    if (path === '/api/staff/google-role-mappings/') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify([]),
      }
    }
    if (
      path === '/api/staff/integration-identities/mcp_readonly/rotate/'
      && options?.method === 'POST'
    ) {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          identity_id: 'mcp_readonly',
          label: 'Read-only MCP',
          username: 'mcp-shared-reader',
          token_created_at: '2026-07-30T18:00:00Z',
          scope_valid: true,
          token: rotatedKey,
        }),
      }
    }
    if (path === '/api/staff/integration-identities/') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify([
          {
            id: 'mcp_readonly',
            label: 'Read-only MCP',
            permission: 'records.read_staff_api',
            exists: true,
            accounts: [{
              username: 'mcp-shared-reader',
              token_created_at: '2026-07-30T17:00:00Z',
              scope_valid: true,
            }],
          },
        ]),
      }
    }
    throw new Error(`Unexpected request: ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={qc}>
      <ToastProvider><IdentitySettings /></ToastProvider>
    </QueryClientProvider>,
  )

  expect(await screen.findByText('mcp-shared-reader')).toBeInTheDocument()
  expect(screen.getByText('Exact scope')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Rotate mcp-shared-reader' }))
  const confirmation = screen.getByRole('dialog', { name: 'Rotate integration token' })
  await user.click(within(confirmation).getByRole('button', { name: 'Rotate token' }))

  expect(await screen.findByText(rotatedKey)).toBeInTheDocument()
  expect(screen.getByText(/will not be shown again/i)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'I stored this token' }))
  await waitFor(() => expect(screen.queryByText(rotatedKey)).not.toBeInTheDocument())
})

it('provisions a missing identity from the console and reveals only its response token', async () => {
  const user = userEvent.setup()
  const issuedKey = 'abcdef0123456789abcdef0123456789abcdef01'
  let provisionBody: unknown
  vi.stubGlobal('fetch', vi.fn(async (
    request: RequestInfo | URL,
    options?: RequestInit,
  ) => {
    const path = String(request)
    if (path === '/api/staff/direct-role-grants/') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify([]),
      }
    }
    if (path === '/api/staff/google-role-mappings/') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify([]),
      }
    }
    if (
      path === '/api/staff/integration-identities/mcp_readonly/provision/'
      && options?.method === 'POST'
    ) {
      provisionBody = JSON.parse(String(options.body))
      return {
        ok: true,
        status: 201,
        text: async () => JSON.stringify({
          identity_id: 'mcp_readonly',
          label: 'Read-only MCP',
          username: 'mcp-shared-reader',
          token_created_at: '2026-07-30T18:00:00Z',
          scope_valid: true,
          token: issuedKey,
        }),
      }
    }
    if (path === '/api/staff/integration-identities/') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify([
          {
            id: 'mcp_readonly',
            label: 'Read-only MCP',
            permission: 'records.read_staff_api',
            exists: false,
            accounts: [],
          },
        ]),
      }
    }
    throw new Error(`Unexpected request: ${path}`)
  }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={qc}>
      <ToastProvider><IdentitySettings /></ToastProvider>
    </QueryClientProvider>,
  )

  await user.click(await screen.findByRole(
    'button',
    { name: 'Provision Read-only MCP' },
  ))
  await user.type(screen.getByLabelText('Service username'), 'mcp-shared-reader')
  await user.click(screen.getByRole('button', { name: 'Provision identity' }))

  expect(await screen.findByText(issuedKey)).toBeInTheDocument()
  expect(provisionBody).toEqual({ username: 'mcp-shared-reader' })
})

function grantsFetch(grants: unknown[], calls: Array<{ url: string; method: string; body: unknown }>) {
  return vi.fn(async (request: RequestInfo | URL, options?: RequestInit) => {
    const path = String(request)
    const method = options?.method ?? 'GET'
    if (method !== 'GET') {
      calls.push({ url: path, method, body: JSON.parse(String(options?.body)) })
      return { ok: true, status: 200, text: async () => JSON.stringify({}) }
    }
    if (path === '/api/staff/direct-role-grants/') {
      return { ok: true, status: 200, text: async () => JSON.stringify(grants) }
    }
    if (path === '/api/staff/google-role-mappings/') {
      return { ok: true, status: 200, text: async () => JSON.stringify([]) }
    }
    if (path === '/api/staff/integration-identities/') {
      return { ok: true, status: 200, text: async () => JSON.stringify([]) }
    }
    throw new Error(`Unexpected fetch: ${path}`)
  })
}

function renderSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <IdentitySettings />
      </ToastProvider>
    </QueryClientProvider>,
  )
}

it('shows who holds a role without a group, and why', async () => {
  vi.stubGlobal('fetch', grantsFetch([{
    id: 7,
    email: 'dana@psd401.net',
    role: 'admin',
    role_label: 'Application Admin',
    active: true,
    reason: 'Records staff who is in no group we can sync.',
    granted_by_username: 'avery',
    created_at: '2026-08-28T10:00:00Z',
    updated_at: '2026-08-28T10:00:00Z',
  }], []))

  renderSettings()

  expect(await screen.findByText('dana@psd401.net')).toBeInTheDocument()
  expect(screen.getByText('Records staff who is in no group we can sync.'))
    .toBeInTheDocument()
  // The reason is the point: an access review has to be able to read it.
  expect(screen.getByText('avery')).toBeInTheDocument()
})

function grant(overrides: Record<string, unknown>) {
  return {
    id: 7,
    email: 'dana@psd401.net',
    role: 'admin',
    role_label: 'Application Admin',
    active: true,
    reason: 'Records staff who is in no group we can sync.',
    granted_by_username: 'avery',
    created_at: '2026-08-28T10:00:00Z',
    updated_at: '2026-08-28T10:00:00Z',
    ...overrides,
  }
}

it('will not submit a direct grant without a substantive reason', async () => {
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  vi.stubGlobal('fetch', grantsFetch([], calls))
  const user = userEvent.setup()

  renderSettings()

  await user.click(await screen.findByRole('button', { name: 'Add direct grant' }))
  const dialog = screen.getByRole('dialog', { name: 'Add direct grant' })
  await user.type(within(dialog).getByLabelText(/^Person/), 'dana@psd401.net')
  await user.click(within(dialog).getByRole('checkbox', { name: 'Application Admin' }))
  await user.type(within(dialog).getByLabelText(/Why this person/), 'too short')

  expect(within(dialog).getByRole('button', { name: 'Save access' })).toBeDisabled()
  expect(calls).toHaveLength(0)
})

it('grants a named individual several roles at once', async () => {
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  vi.stubGlobal('fetch', grantsFetch([], calls))
  const user = userEvent.setup()

  renderSettings()

  await user.click(await screen.findByRole('button', { name: 'Add direct grant' }))
  const dialog = screen.getByRole('dialog', { name: 'Add direct grant' })
  await user.type(within(dialog).getByLabelText(/^Person/), 'dana@psd401.net')
  await user.click(within(dialog).getByRole('checkbox', { name: 'Records Officer' }))
  await user.click(within(dialog).getByRole('checkbox', { name: 'Application Admin' }))
  await user.type(
    within(dialog).getByLabelText(/Why this person/),
    'Records staff who is in no group we can sync.',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Save access' }))

  await waitFor(() => expect(calls).toHaveLength(2))
  expect(calls.map((call) => [call.method, call.url])).toEqual([
    ['POST', '/api/staff/direct-role-grants/'],
    ['POST', '/api/staff/direct-role-grants/'],
  ])
  expect(calls.map((call) => (call.body as { role: string }).role))
    .toEqual(['records_officer', 'admin'])
  expect(at(calls, 0).body).toMatchObject({
    email: 'dana@psd401.net',
    reason: 'Records staff who is in no group we can sync.',
    active: true,
  })
})

it('shows one row per person with every role they hold', async () => {
  vi.stubGlobal('fetch', grantsFetch([
    grant({}),
    grant({ id: 8, role: 'records_officer', role_label: 'Records Officer' }),
    grant({ id: 9, role: 'liaison', role_label: 'Department Liaison', active: false }),
  ], []))

  renderSettings()

  const cells = await screen.findAllByText('dana@psd401.net')
  expect(cells).toHaveLength(1)
  expect(screen.getByText('Records Officer, Application Admin')).toBeInTheDocument()
})

it('adds, restores, and removes only the roles the admin changed', async () => {
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  vi.stubGlobal('fetch', grantsFetch([
    grant({}),
    grant({ id: 8, role: 'reviewer', role_label: 'Reviewer' }),
    grant({ id: 9, role: 'liaison', role_label: 'Department Liaison', active: false }),
  ], calls))
  const user = userEvent.setup()

  renderSettings()

  await user.click(await screen.findByText('dana@psd401.net'))
  const dialog = screen.getByRole('dialog', { name: 'Edit access for dana@psd401.net' })
  expect(within(dialog).getByRole('checkbox', { name: 'Application Admin' })).toBeChecked()
  expect(within(dialog).getByRole('checkbox', { name: 'Department Liaison' })).not.toBeChecked()
  await user.click(within(dialog).getByRole('checkbox', { name: 'Records Officer' }))
  await user.click(within(dialog).getByRole('checkbox', { name: 'Department Liaison' }))
  await user.click(within(dialog).getByRole('checkbox', { name: 'Reviewer' }))
  await user.click(within(dialog).getByRole('button', { name: 'Save access' }))

  // Additions first, so a swap never passes through a state with less access.
  await waitFor(() => expect(calls).toHaveLength(3))
  expect(calls[0]).toMatchObject({
    method: 'POST',
    url: '/api/staff/direct-role-grants/',
    body: { email: 'dana@psd401.net', role: 'records_officer', active: true },
  })
  expect(calls[1]).toMatchObject({
    method: 'PATCH',
    url: '/api/staff/direct-role-grants/9/',
    body: { active: true },
  })
  expect(calls[2]).toEqual({
    method: 'PATCH',
    url: '/api/staff/direct-role-grants/8/',
    body: { active: false },
  })
})

it('deletes every direct grant a person holds after confirmation', async () => {
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  const fetchMock = grantsFetch([
    grant({}),
    grant({ id: 8, role: 'reviewer', role_label: 'Reviewer', active: false }),
  ], calls)
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, options?: RequestInit) => {
    if (options?.method === 'DELETE') {
      calls.push({ url: String(request), method: 'DELETE', body: null })
      return { ok: true, status: 204, text: async () => '' }
    }
    return fetchMock(request, options)
  }))
  const user = userEvent.setup()

  renderSettings()

  await user.click(await screen.findByText('dana@psd401.net'))
  const dialog = screen.getByRole('dialog', { name: 'Edit access for dana@psd401.net' })
  await user.click(within(dialog).getByRole('button', { name: 'Delete all grants' }))
  expect(calls).toHaveLength(0)
  const confirm = screen.getByRole('dialog', { name: 'Delete direct grants' })
  await user.click(within(confirm).getByRole('button', { name: 'Delete grants' }))

  await waitFor(() => expect(calls).toHaveLength(2))
  expect(calls.map((call) => [call.method, call.url])).toEqual([
    ['DELETE', '/api/staff/direct-role-grants/7/'],
    ['DELETE', '/api/staff/direct-role-grants/8/'],
  ])
})

it('saves a corrected reason on roles that stay ticked', async () => {
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  vi.stubGlobal('fetch', grantsFetch([grant({})], calls))
  const user = userEvent.setup()

  renderSettings()

  await user.click(await screen.findByText('dana@psd401.net'))
  const dialog = screen.getByRole('dialog', { name: 'Edit access for dana@psd401.net' })
  const reason = within(dialog).getByLabelText(/Why this person/)
  await user.clear(reason)
  await user.type(reason, 'District Records Officer with the same access as the administrator.')
  await user.click(within(dialog).getByRole('button', { name: 'Save access' }))

  await waitFor(() => expect(calls).toHaveLength(1))
  expect(calls[0]).toEqual({
    method: 'PATCH',
    url: '/api/staff/direct-role-grants/7/',
    body: { reason: 'District Records Officer with the same access as the administrator.' },
  })
})

it('will not add a person without at least one role', async () => {
  vi.stubGlobal('fetch', grantsFetch([], []))
  const user = userEvent.setup()

  renderSettings()

  await user.click(await screen.findByRole('button', { name: 'Add direct grant' }))
  const dialog = screen.getByRole('dialog', { name: 'Add direct grant' })
  await user.type(within(dialog).getByLabelText(/^Person/), 'dana@psd401.net')
  await user.type(
    within(dialog).getByLabelText(/Why this person/),
    'Records staff who is in no group we can sync.',
  )

  expect(within(dialog).getByRole('button', { name: 'Save access' })).toBeDisabled()
})
