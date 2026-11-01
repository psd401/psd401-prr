// ABOUTME: Agent Access tests cover PAT lifecycle, errors, expiry, and live policy.
// ABOUTME: Secrets are returned once and never leak into later metadata renders.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { AgentAccess } from './AgentAccess'

interface MockToken {
  id: number
  name: string
  prefix: string
  created_at: string
  expires_at: string
  last_used_at: string | null
  revoked_at: string | null
}

const capabilityCatalog = {
  policy: {
    direct: 'Agent may execute through the attributed REST API.',
    human_confirmation: 'A human must explicitly approve execution.',
    human_only: 'The accountable human executes this action in the staff UI.',
  },
  capabilities: [
    {
      id: 'requests.list',
      outcome: 'Browse the request queue.',
      execution: 'direct',
    },
    {
      id: 'requests.transition',
      outcome: 'Apply an authorized lifecycle transition.',
      execution: 'human_confirmation',
    },
    {
      id: 'correspondence.approve',
      outcome: 'Approve requester correspondence.',
      execution: 'human_only',
    },
  ],
}

function response(status: number, payload?: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => payload === undefined ? '' : JSON.stringify(payload),
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <AgentAccess />
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Agent Access personal token lifecycle', () => {
  it('shows create and rotate secrets once, then revokes retained metadata', async () => {
    const user = userEvent.setup()
    const clipboardWrite = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWrite },
    })
    let rows: MockToken[] = []
    let nextId = 1

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const method = init?.method ?? 'GET'
      if (path === '/api/staff/capabilities/') {
        return response(200, capabilityCatalog)
      }
      if (path === '/api/staff/auth/api-tokens/' && method === 'GET') {
        return response(200, rows)
      }
      if (path === '/api/staff/auth/api-tokens/' && method === 'POST') {
        const body = JSON.parse(String(init?.body))
        const row: MockToken = {
          id: nextId++,
          name: body.name,
          prefix: 'createprefix',
          created_at: '2026-07-30T17:00:00Z',
          expires_at: '2026-08-29T17:00:00Z',
          last_used_at: null,
          revoked_at: null,
        }
        rows = [row]
        return response(201, { ...row, token: 'prr_pat_createprefix_FIRST_SECRET' })
      }
      if (path === '/api/staff/auth/api-tokens/1/rotate' && method === 'POST') {
        rows = rows.map((row) => (
          row.id === 1 ? { ...row, revoked_at: '2026-07-30T18:00:00Z' } : row
        ))
        const row: MockToken = {
          id: nextId++,
          name: 'Claude Desktop',
          prefix: 'rotateprefix',
          created_at: '2026-07-30T18:00:00Z',
          expires_at: '2026-08-29T18:00:00Z',
          last_used_at: null,
          revoked_at: null,
        }
        rows = [row, ...rows]
        return response(200, { ...row, token: 'prr_pat_rotateprefix_SECOND_SECRET' })
      }
      if (path === '/api/staff/auth/api-tokens/2' && method === 'DELETE') {
        rows = rows.map((row) => (
          row.id === 2 ? { ...row, revoked_at: '2026-07-30T19:00:00Z' } : row
        ))
        return response(204)
      }
      throw new Error(`Unexpected fetch: ${method} ${path}`)
    }))

    renderPage()
    await screen.findByText('No personal access tokens yet.')

    await user.type(screen.getByLabelText('Token name'), 'Claude Desktop')
    await user.clear(screen.getByLabelText('Lifetime (days)'))
    await user.type(screen.getByLabelText('Lifetime (days)'), '30')
    await user.click(screen.getByRole('button', { name: 'Create token' }))

    expect(await screen.findByText('prr_pat_createprefix_FIRST_SECRET')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Copy token' }))
    expect(clipboardWrite).toHaveBeenCalledWith('prr_pat_createprefix_FIRST_SECRET')
    await user.click(screen.getByRole('button', { name: 'I saved this token' }))
    expect(screen.queryByText('prr_pat_createprefix_FIRST_SECRET')).not.toBeInTheDocument()
    expect(await screen.findByText('Claude Desktop')).toBeInTheDocument()
    expect(screen.getByText('createprefix')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Rotate Claude Desktop' }))
    await user.click(screen.getByRole('button', { name: 'Confirm rotate' }))
    expect(await screen.findByText('prr_pat_rotateprefix_SECOND_SECRET')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'I saved this token' }))
    expect(screen.queryByText('prr_pat_rotateprefix_SECOND_SECRET')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Revoke Claude Desktop' }))
    await user.click(screen.getByRole('button', { name: 'Confirm revoke' }))
    await waitFor(() => {
      const tokenRow = screen.getByText('rotateprefix').closest('tr')
      expect(tokenRow).not.toBeNull()
      expect(within(tokenRow!).getByText('Revoked')).toBeInTheDocument()
    })
  })

  it('renders lifetime, active-limit, and duplicate-name API failures by the form', async () => {
    const user = userEvent.setup()
    let attempts = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const method = init?.method ?? 'GET'
      if (path === '/api/staff/capabilities/') return response(200, capabilityCatalog)
      if (path === '/api/staff/auth/api-tokens/' && method === 'GET') {
        return response(200, [])
      }
      if (path === '/api/staff/auth/api-tokens/' && method === 'POST') {
        attempts += 1
        if (attempts === 1) {
          return response(400, { lifetime_days: ['Must not exceed 90 days.'] })
        }
        if (attempts === 2) {
          return response(400, { non_field_errors: ['You already have 5 active tokens.'] })
        }
        return response(400, {
          name: ['You already have a non-revoked token with this name.'],
        })
      }
      throw new Error(`Unexpected fetch: ${method} ${path}`)
    }))

    renderPage()
    await screen.findByText('No personal access tokens yet.')
    await user.type(screen.getByLabelText('Token name'), 'Too long')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByText('Must not exceed 90 days.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByText('You already have 5 active tokens.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(
      await screen.findByText('You already have a non-revoked token with this name.'),
    ).toBeInTheDocument()
  })

  it.each([
    {
      action: 'rotate',
      openLabel: 'Rotate Claude Desktop',
      confirmLabel: 'Confirm rotate',
      mutationPath: '/api/staff/auth/api-tokens/1/rotate',
      mutationMethod: 'POST',
      failure: 'Token rotation is temporarily unavailable.',
    },
    {
      action: 'revoke',
      openLabel: 'Revoke Claude Desktop',
      confirmLabel: 'Confirm revoke',
      mutationPath: '/api/staff/auth/api-tokens/1',
      mutationMethod: 'DELETE',
      failure: 'Token revocation is temporarily unavailable.',
    },
  ])('keeps the $action confirmation and eventual failure visible while pending', async ({
    openLabel,
    confirmLabel,
    mutationPath,
    mutationMethod,
    failure,
  }) => {
    const user = userEvent.setup()
    const mutation = deferred<ReturnType<typeof response>>()
    const token: MockToken = {
      id: 1,
      name: 'Claude Desktop',
      prefix: 'activeprefix',
      created_at: '2026-07-30T17:00:00Z',
      expires_at: '2026-08-29T17:00:00Z',
      last_used_at: null,
      revoked_at: null,
    }

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const method = init?.method ?? 'GET'
      if (path === '/api/staff/capabilities/') return response(200, capabilityCatalog)
      if (path === '/api/staff/auth/api-tokens/' && method === 'GET') {
        return response(200, [token])
      }
      if (path === mutationPath && method === mutationMethod) return mutation.promise
      throw new Error(`Unexpected fetch: ${method} ${path}`)
    }))

    renderPage()
    await user.click(await screen.findByRole('button', { name: openLabel }))
    const dialog = screen.getByRole('dialog') as HTMLDialogElement
    const confirm = within(dialog).getByRole('button', { name: confirmLabel })
    await user.click(confirm)
    await waitFor(() => expect(confirm).toBeDisabled())

    const cancelEvent = new Event('cancel', { cancelable: true })
    expect(dialog.dispatchEvent(cancelEvent)).toBe(false)
    expect(cancelEvent.defaultPrevented).toBe(true)

    fireEvent.click(dialog)
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' })
    expect(cancel).toBeDisabled()
    await user.click(cancel)
    act(() => dialog.close())

    expect(dialog).toHaveAttribute('open')
    expect(screen.getByRole('dialog')).toBe(dialog)

    mutation.resolve(response(503, { detail: failure }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(failure)
    expect(dialog).toHaveAttribute('open')
    expect(cancel).toBeEnabled()
  })

  it('visually distinguishes expired and expiring-soon token rows', async () => {
    vi.setSystemTime(new Date('2026-07-30T17:00:00Z'))
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/staff/capabilities/') return response(200, capabilityCatalog)
      if (path === '/api/staff/auth/api-tokens/') {
        return response(200, [
          {
            id: 1,
            name: 'Expired token',
            prefix: 'expiredprefix',
            created_at: '2026-07-01T17:00:00Z',
            expires_at: '2026-07-29T17:00:00Z',
            last_used_at: null,
            revoked_at: null,
          },
          {
            id: 2,
            name: 'Soon token',
            prefix: 'soonprefix',
            created_at: '2026-07-01T17:00:00Z',
            expires_at: '2026-08-02T17:00:00Z',
            last_used_at: '2026-07-30T16:00:00Z',
            revoked_at: null,
          },
        ])
      }
      throw new Error(`Unexpected fetch: ${path}`)
    }))

    renderPage()
    expect(await screen.findByText('Expired')).toBeInTheDocument()
    expect(screen.getByText('Expires soon')).toBeInTheDocument()
  })
})

describe('Agent Access discovery', () => {
  it('groups the live policy and renders safe current-origin connection snippets', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/staff/capabilities/') return response(200, capabilityCatalog)
      if (path === '/api/staff/auth/api-tokens/') return response(200, [])
      throw new Error(`Unexpected fetch: ${path}`)
    }))

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Direct' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Human confirmation' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Human only' })).toBeInTheDocument()
    expect(screen.getByText('Browse the request queue.')).toBeInTheDocument()
    expect(screen.getByText('Approve requester correspondence.')).toBeInTheDocument()

    expect(document.body).toHaveTextContent(`${window.location.origin}/mcp`)
    expect(document.body).toHaveTextContent(
      'The hosted /mcp transport is available for clients that can send the personal-token header.',
    )
    expect(document.body).not.toHaveTextContent('once #44 defines its authentication flow')
    expect(document.body).toHaveTextContent(`PRR_BASE_URL=${window.location.origin}`)
    expect(screen.getAllByText(/YOUR_PERSONAL_TOKEN/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/FIRST_SECRET|SECOND_SECRET/)).not.toBeInTheDocument()
    const snippets = Array.from(document.querySelectorAll('pre code')).map(
      (element) => element.textContent ?? '',
    )
    const stdioSnippet = snippets.find((snippet) => (
      snippet.includes('integrations/mcp/server.py')
      && snippet.startsWith('claude mcp add')
    ))
    expect(stdioSnippet).toMatch(/^claude mcp add --transport stdio \\\n/)
    expect(stdioSnippet).toContain('\n  --env PRR_BASE_URL=')
    expect(stdioSnippet).toContain('\n  prr-staff \\\n  -- uv run')
    const desktopSnippet = snippets.find((snippet) => (
      snippet.includes('"mcpServers"')
    ))
    expect(JSON.parse(desktopSnippet ?? '')).toEqual({
      mcpServers: {
        'prr-staff': {
          command: 'uv',
          args: ['run', '/absolute/path/to/integrations/mcp/server.py'],
          env: {
            PRR_BASE_URL: window.location.origin,
            PRR_API_TOKEN: 'YOUR_PERSONAL_TOKEN',
          },
        },
      },
    })
    expect(screen.getByRole('link', { name: 'Swagger API docs' })).toHaveAttribute('href', '/api/docs/')
    expect(screen.getByRole('link', { name: 'ReDoc API docs' })).toHaveAttribute('href', '/api/redoc/')
    expect(screen.getByRole('link', { name: 'Raw capability catalog' })).toHaveAttribute(
      'href',
      '/api/staff/capabilities/',
    )
  })
})
