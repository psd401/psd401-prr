// ABOUTME: Regression reproductions for destructive identity and credential actions.
// ABOUTME: Destructive writes need confirmation and one-time secrets need safe handling.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ReactElement } from 'react'
import { ToastProvider } from '../ui'
import { AgentAccess } from './AgentAccess'
import { IdentitySettings } from './IdentitySettings'

function response(body?: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => body === undefined ? '' : JSON.stringify(body),
  } as Response
}

function renderPage(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>{element}</ToastProvider>
    </QueryClientProvider>,
  )
}

const capabilityCatalog = {
  policy: {
    direct: 'Direct execution.',
    human_confirmation: 'Human confirmation required.',
    human_only: 'Human-only action.',
  },
  capabilities: [],
}

const activeToken = {
  id: 17,
  name: 'Records agent',
  prefix: 'recordsagent',
  created_at: '2026-07-30T17:00:00Z',
  expires_at: '2026-08-30T17:00:00Z',
  last_used_at: null,
  revoked_at: null,
}

function missingIntegrationIdentity() {
  return [{
    id: 'mcp_readonly',
    label: 'Read-only MCP',
    permission: 'records.read_staff_api',
    exists: false,
    accounts: [],
  }]
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Credential deletion and rotation require confirmation', () => {
  it('confirms a Google role-mapping deletion before issuing DELETE', async () => {
    let deleteCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/direct-role-grants/' && method === 'GET') {
        return response([])
      }
      if (url === '/api/staff/google-role-mappings/' && method === 'GET') {
        return response({
          count: 1,
          next: null,
          previous: null,
          results: [{
            id: 3,
            role: 'liaison',
            role_label: 'Department Liaison',
            google_group: 'records-liaisons@psd401.net',
            active: true,
            created_at: '2026-07-01T00:00:00Z',
            updated_at: '2026-07-01T00:00:00Z',
          }],
        })
      }
      if (url === '/api/staff/integration-identities/') return response([])
      if (url === '/api/staff/google-role-mappings/3/' && method === 'DELETE') {
        deleteCalls += 1
        return response(undefined, 204)
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    renderPage(<IdentitySettings />)
    const group = await screen.findByText('records-liaisons@psd401.net')
    await user.click(group.closest('tr')!)
    await user.click(
      within(screen.getByRole('dialog', { name: 'Edit role mapping' }))
        .getByRole('button', { name: 'Delete' }),
    )

    expect(deleteCalls).toBe(0)
    const confirmation = screen.getByRole('dialog', { name: /delete role mapping/i })
    await user.click(within(confirmation).getByRole('button', { name: /confirm delete/i }))
    await waitFor(() => expect(deleteCalls).toBe(1))
  })

  it('confirms personal-token rotation before revoking the current credential', async () => {
    let rotateCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/capabilities/') return response(capabilityCatalog)
      if (url === '/api/staff/auth/api-tokens/' && method === 'GET') {
        return response([activeToken])
      }
      if (url === '/api/staff/auth/api-tokens/17/rotate' && method === 'POST') {
        rotateCalls += 1
        return response({
          ...activeToken,
          id: 18,
          prefix: 'replacement',
          token: 'prr_pat_replacement_secret',
        })
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    renderPage(<AgentAccess />)
    await user.click(await screen.findByRole('button', { name: 'Rotate Records agent' }))

    expect(rotateCalls).toBe(0)
    const confirmation = screen.getByRole('dialog', {
      name: /rotate personal access token/i,
    })
    await user.click(within(confirmation).getByRole('button', { name: /confirm rotate/i }))
    await waitFor(() => expect(rotateCalls).toBe(1))
  })

  it('confirms personal-token revocation before issuing DELETE', async () => {
    let revokeCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/capabilities/') return response(capabilityCatalog)
      if (url === '/api/staff/auth/api-tokens/' && method === 'GET') {
        return response([activeToken])
      }
      if (url === '/api/staff/auth/api-tokens/17' && method === 'DELETE') {
        revokeCalls += 1
        return response(undefined, 204)
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    renderPage(<AgentAccess />)
    await user.click(await screen.findByRole('button', { name: 'Revoke Records agent' }))

    expect(revokeCalls).toBe(0)
    const confirmation = screen.getByRole('dialog', {
      name: /revoke personal access token/i,
    })
    await user.click(within(confirmation).getByRole('button', { name: /confirm revoke/i }))
    await waitFor(() => expect(revokeCalls).toBe(1))
  })
})

describe('A one-time integration token is never discarded silently', () => {
  async function provisionCredential() {
    const secret = '0123456789abcdef0123456789abcdef0123456789'
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/staff/direct-role-grants/') return response([])
      if (url === '/api/staff/google-role-mappings/') return response([])
      if (url === '/api/staff/integration-identities/') {
        return response(missingIntegrationIdentity())
      }
      if (
        url === '/api/staff/integration-identities/mcp_readonly/provision/'
        && method === 'POST'
      ) {
        return response({
          identity_id: 'mcp_readonly',
          label: 'Read-only MCP',
          username: 'mcp-shared-reader',
          token_created_at: '2026-07-31T12:00:00Z',
          scope_valid: true,
          token: secret,
        }, 201)
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    renderPage(<IdentitySettings />)
    await user.click(await screen.findByRole('button', {
      name: 'Provision Read-only MCP',
    }))
    await user.type(screen.getByLabelText('Service username'), 'mcp-shared-reader')
    await user.click(screen.getByRole('button', { name: 'Provision identity' }))
    expect(await screen.findByText(secret)).toBeInTheDocument()
    return { secret, user }
  }

  it('provides an explicit copy action for a newly issued integration token', async () => {
    const clipboardWrite = vi.fn(async () => undefined)
    const { secret, user } = await provisionCredential()
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWrite },
    })

    await user.click(screen.getByRole('button', { name: /copy.*token/i }))
    expect(clipboardWrite).toHaveBeenCalledWith(secret)
  })

  it('does not discard a one-time integration token when its dialog backdrop is clicked', async () => {
    const { secret } = await provisionCredential()
    const dialog = screen.getByRole('dialog', { name: 'Store integration token now' })

    fireEvent.click(dialog)

    expect(screen.getByText(secret)).toBeInTheDocument()
    expect(dialog).toHaveAttribute('open')
  })

  it('prevents Escape from hiding a one-time integration token', async () => {
    const { secret } = await provisionCredential()
    const dialog = screen.getByRole('dialog', {
      name: 'Store integration token now',
    })
    const cancelEvent = new Event('cancel', { cancelable: true })

    const accepted = dialog.dispatchEvent(cancelEvent)

    expect(accepted).toBe(false)
    expect(cancelEvent.defaultPrevented).toBe(true)
    expect(screen.getByText(secret)).toBeInTheDocument()
    expect(dialog).toHaveAttribute('open')
  })
})
