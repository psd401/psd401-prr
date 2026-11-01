// ABOUTME: Session and route-policy tests for public, authorized, and denied screens.
// ABOUTME: A denied route must never mount its data-fetching child component.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from './App'
import type { StaffRole } from './api/auth'
import { CONFIGURATION_NAV, OVERSIGHT_NAV, PRIMARY_NAV } from './nav'
import { ToastProvider } from './ui'

function response(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  } as Response
}

function staffUser(roles: StaffRole[]) {
  return {
    authenticated: true as const,
    username: 'staff-user',
    email: 'staff-user@psd401.net',
    first_name: 'Staff',
    last_name: 'User',
    is_superuser: false,
    roles,
  }
}

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider><App /></ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  window.history.replaceState({}, '', '/')
  vi.unstubAllGlobals()
})

it.each([
  '/configuration/agent-access',
  '/agent-access',
])('renders canonical personal Agent Access for an Intake Clerk at %s', async (path) => {
  window.history.replaceState({}, '', path)
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const requestPath = String(input)
    if (requestPath === '/api/staff/auth/me') return response(200, staffUser(['intake_clerk']))
    if (requestPath === '/api/staff/auth/api-tokens/') return response(200, [])
    if (requestPath === '/api/staff/capabilities/') {
      return response(200, {
        policy: { direct: 'Direct.', human_confirmation: 'Confirm.', human_only: 'Handoff.' },
        capabilities: [],
      })
    }
    throw new Error(`Unexpected fetch: ${requestPath}`)
  }))

  renderApp()

  expect(await screen.findByRole('heading', { name: 'Agent access', level: 1 })).toBeInTheDocument()
  await waitFor(() => expect(window.location.pathname).toBe('/configuration/agent-access'))
})

it('redirects the legacy global Correspondence route away from a Liaison', async () => {
  window.history.replaceState({}, '', '/correspondence')
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const requestPath = String(input)
    requests.push(requestPath)
    if (requestPath === '/api/staff/auth/me') {
      return response(200, staffUser(['liaison']))
    }
    if (requestPath === '/api/staff/work-items/counts/') return response(200, {})
    if (requestPath === '/api/staff/requests/') {
      return response(200, { count: 0, next: null, previous: null, results: [] })
    }
    throw new Error(`Unexpected fetch: ${requestPath}`)
  }))

  renderApp()

  expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
  await waitFor(() => expect(window.location.pathname).toBe('/inbox'))
  expect(requests.some((requestPath) => requestPath.startsWith('/api/staff/correspondence/')))
    .toBe(false)
})

it('sends the retired My Work URL to its personal queue in the Inbox', async () => {
  window.history.replaceState({}, '', '/my-work')
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const requestPath = String(input)
    if (requestPath === '/api/staff/auth/me') {
      return response(200, staffUser(['records_officer']))
    }
    if (requestPath === '/api/staff/work-items/counts/') return response(200, {})
    if (requestPath === '/api/staff/work-items/?queue=personal') {
      return response(200, { count: 0, next: null, previous: null, results: [] })
    }
    throw new Error(`Unexpected fetch: ${requestPath}`)
  }))

  renderApp()

  expect(await screen.findByRole('heading', { name: 'Inbox', level: 1 }))
    .toBeInTheDocument()
  await waitFor(() => expect(window.location.pathname).toBe('/inbox'))
  expect(window.location.search).toBe('?view=personal')
})

it('does not mount Agent Access for an anonymous visitor', async () => {
  window.history.replaceState({}, '', '/configuration/agent-access')
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/staff/auth/me') {
      return response(403, { detail: 'Authentication credentials were not provided.' })
    }
    throw new Error(`Agent Access mounted unexpectedly: ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderApp()

  expect(await screen.findByText('Staff workspace sign-in')).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it.each([
  ['/request', 'Request public records'],
  ['/request/submitted?number=PRR-2026-0123', 'Request submitted'],
])('renders public route %s without checking the staff session', async (path, heading) => {
  window.history.replaceState({}, '', path)
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    throw new Error(`Public request route unexpectedly fetched: ${String(input)}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderApp()

  expect(await screen.findByRole('heading', { name: heading, level: 1 })).toBeInTheDocument()
  expect(fetchMock).not.toHaveBeenCalled()
})

it('gives a Reviewer the Inbox case index and no message queues', async () => {
  window.history.replaceState({}, '', '/inbox')
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const requestPath = String(input)
    requests.push(requestPath)
    if (requestPath === '/api/staff/auth/me') {
      return response(200, staffUser(['reviewer']))
    }
    if (requestPath === '/api/staff/work-items/counts/') return response(200, {})
    if (requestPath.startsWith('/api/staff/requests/')) {
      return response(200, { count: 0, next: null, previous: null, results: [] })
    }
    throw new Error(`Unexpected fetch: ${requestPath}`)
  }))

  renderApp()

  expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
  // The case index is the only queue a reviewer gets. The cross-case message
  // and work queues are not merely hidden -- they are never requested.
  expect(await screen.findByRole('button', { name: /All cases/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Needs attention/ })).not.toBeInTheDocument()
  await waitFor(() =>
    expect(requests.some((path) => path.startsWith('/api/staff/requests/'))).toBe(true),
  )
  expect(requests.some((path) => path.startsWith('/api/staff/correspondence/'))).toBe(false)
  expect(requests.some((path) => path.includes('/work-items/?queue='))).toBe(false)
})

it.each([
  ['Intake Clerk', ['intake_clerk'], '/cases', '/api/staff/requests/'],
  ['Liaison', ['liaison'], '/review', '/api/staff/work-items/?queue=review'],
  ['Intake Clerk', ['intake_clerk'], '/configuration/identity', '/api/staff/role-mappings/'],
  ['Liaison', ['liaison'], '/configuration/correspondence', '/api/staff/correspondence-operations/'],
  ['Application Admin', ['admin'], '/review', '/api/staff/work-items/?queue=review'],
] as const)(
  '%s sees Not authorized at %s without mounting its child',
  async (_name, roles, path, forbiddenPrefix) => {
    window.history.replaceState({}, '', path)
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const requestPath = String(input)
      requests.push(requestPath)
      if (requestPath === '/api/staff/auth/me') {
        return response(200, staffUser([...roles] as StaffRole[]))
      }
      if (requestPath === '/api/staff/work-items/counts/') return response(200, {})
      throw new Error(`Denied screen fetched unexpectedly: ${requestPath}`)
    }))

    renderApp()

    expect(await screen.findByRole('heading', { name: 'Not authorized' })).toBeInTheDocument()
    expect(requests.some((requestPath) => requestPath.startsWith(forbiddenPrefix))).toBe(false)
  },
)

const STAFF_ROUTES = [
  ...PRIMARY_NAV.map((item) => item.path),
  ...CONFIGURATION_NAV.map((item) => item.path),
  ...OVERSIGHT_NAV.map((item) => item.path),
  '/requests/PRR-2026-0001',
]

const ROLE_FIXTURES: Array<[string, StaffRole]> = [
  ['Records Officer', 'records_officer'],
  ['Department Liaison', 'liaison'],
  ['Reviewer', 'reviewer'],
  ['Intake Clerk', 'intake_clerk'],
  ['Application Admin', 'admin'],
]

const DIRECT_ROUTE_DENIALS = [
  ...PRIMARY_NAV,
  ...CONFIGURATION_NAV,
  ...OVERSIGHT_NAV,
].flatMap((item) => ROLE_FIXTURES
  .filter(([, role]) => !item.roles.includes(role))
  .map(([roleName, role]) => [roleName, item.path, role] as const))

it.each(DIRECT_ROUTE_DENIALS)(
  '%s is denied at direct route %s before its child mounts',
  async (_roleName, path, role) => {
    window.history.replaceState({}, '', path)
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const requestPath = String(input)
      requests.push(requestPath)
      if (requestPath === '/api/staff/auth/me') {
        return response(200, staffUser([role]))
      }
      if (requestPath === '/api/staff/work-items/counts/') return response(200, {})
      throw new Error(`Denied screen fetched unexpectedly: ${requestPath}`)
    }))

    renderApp()

    expect(await screen.findByRole('heading', { name: 'Not authorized' })).toBeInTheDocument()
    expect(requests.every((requestPath) => (
      requestPath === '/api/staff/auth/me'
      || requestPath === '/api/staff/work-items/counts/'
    ))).toBe(true)
  },
)

it.each(STAFF_ROUTES)('keeps staff route %s behind the session gate', async (path) => {
  window.history.replaceState({}, '', path)
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/auth/me') {
      return response(403, { detail: 'Authentication credentials were not provided.' })
    }
    throw new Error(`Staff screen mounted before authentication: ${String(input)}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderApp()

  expect(await screen.findByText('Staff workspace sign-in')).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})
