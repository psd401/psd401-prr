import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { MessageTemplates } from './MessageTemplates'

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

const ACTIVE = {
  id: null,
  kind: 'acknowledgment',
  template_version: '2',
  state: 'active',
  source: 'built_in',
  subject_template: 'Public Records Request {{ request_number }} — Acknowledgment',
  body_template: 'RCW 42.56.520 {{ requester_name }} {{ request_number }} {{ received_date }} {{ request_description }} {{ estimated_completion }} {{ pra_officer_email }}',
  change_reason: 'Source-controlled application default.',
  created_by_username: null,
  activated_by_username: null,
  created_at: null,
  updated_at: null,
  activated_at: null,
  revision: null,
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <MessageTemplates roles={['records_officer']} />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('MessageTemplates', () => {
  it('shows the active five-day letter and creates a governed editable version', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/automatic-message-templates/') && (!init?.method || init.method === 'GET')) {
        return response({
          templates: [{
            kind: 'acknowledgment',
            label: 'Five-day acknowledgment',
            group: 'Statutory correspondence',
            authoring: 'structured',
            allowed_tokens: ['estimated_completion', 'request_number'],
            required_tokens: ['estimated_completion', 'request_number'],
            active: ACTIVE,
            drafts: [],
            history: [],
          }],
        })
      }
      if (url.endsWith('/automatic-message-templates/') && init?.method === 'POST') {
        return response({ ...ACTIVE, id: 'template-1', state: 'draft', source: 'managed', revision: 1 }, 201)
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderPage()
    expect(await screen.findByRole('heading', { name: 'Five-day acknowledgment' }))
      .toBeInTheDocument()
    expect(screen.getByText('Built-in application default')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create editable version' }))
    await user.type(
      screen.getByLabelText(/Change reason/),
      'Prepare the district wording for accountable activation.',
    )
    await user.click(screen.getByRole('button', { name: 'Save draft' }))

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/staff/automatic-message-templates/',
      expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('"kind":"acknowledgment"'),
      }),
    )
  })
  it('distinguishes structured templates from staff-authored starting points', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/automatic-message-templates/')) {
        return response({
          templates: [
            {
              kind: 'acknowledgment',
              label: 'Five-day acknowledgment',
              group: 'Statutory correspondence',
              authoring: 'structured',
              allowed_tokens: ['request_number'],
              required_tokens: ['request_number'],
              active: ACTIVE,
              drafts: [],
              history: [],
            },
            {
              kind: 'denial',
              label: 'Denial (starting point)',
              group: 'Staff-authored starting points',
              authoring: 'free_form',
              allowed_tokens: ['request_number'],
              required_tokens: ['request_number'],
              active: {
                ...ACTIVE,
                kind: 'denial',
                template_version: 'default',
                subject_template: 'Public Records Request {{ request_number }} — Denial',
                body_template: 'Dear {{ requester_name }}, {{ request_number }}',
              },
              drafts: [],
              history: [],
            },
          ],
        })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Message templates' }))
      .toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Denial (starting point)' }))
      .toBeInTheDocument()
    expect(screen.getByText('Structured template')).toBeInTheDocument()
    expect(screen.getByText('Starting point')).toBeInTheDocument()
    expect(screen.getByText('Staff-authored starting points')).toBeInTheDocument()
    expect(screen.getByText(/never changes who may send a message/))
      .toBeInTheDocument()
    expect(
      screen.getAllByRole('button', { name: 'Create editable version' }),
    ).toHaveLength(2)
  })
})

describe('MessageTemplates guardrails', () => {
  const CATALOG = {
    templates: [{
      kind: 'acknowledgment',
      label: 'Five-day acknowledgment',
      group: 'Statutory correspondence',
      authoring: 'structured',
      allowed_tokens: ['estimated_completion', 'request_number'],
      required_tokens: ['request_number'],
      active: ACTIVE,
      drafts: [],
      history: [],
    }],
  }

  const WARNING_409 = {
    detail: 'This template does not meet a guardrail.',
    code: 'guardrail_override_required',
    may_override: true,
    guardrail_warnings: [{
      code: 'missing_phrase:rcw 42.56.520',
      message: 'The template no longer contains “rcw 42.56.520”.',
      consequence: 'The five-day response must cite the statute that requires the estimate.',
    }],
  }

  async function openEditorWith(fetchMock: ReturnType<typeof vi.fn>) {
    vi.stubGlobal('fetch', fetchMock)
    renderPage()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /edit/i }))
    await user.type(
      screen.getByRole('textbox', { name: /change reason/i }),
      'Adopt the wording the district actually uses.',
    )
    return user
  }

  it('states what the requester loses and lets an officer publish anyway', async () => {
    let overrideSent: string | undefined
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/automatic-message-templates/') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as Record<string, string>
        if (!body.guardrail_override_reason) return response(WARNING_409, 409)
        overrideSent = body.guardrail_override_reason
        return response({ ...ACTIVE, id: 't1', state: 'draft', source: 'managed', revision: 1 }, 201)
      }
      return response(CATALOG)
    })
    const user = await openEditorWith(fetchMock)

    await user.click(screen.getByRole('button', { name: 'Save draft' }))

    // The consequence, not the rule: an officer can act on the first and not
    // the second.
    expect(
      await screen.findByText(/must cite the statute that requires the estimate/i),
    ).toBeInTheDocument()

    await user.type(
      screen.getByRole('textbox', { name: /why publish it anyway/i }),
      'Counsel approved this exact wording on review.',
    )
    await user.click(screen.getByRole('button', { name: 'Save draft anyway' }))

    expect(overrideSent).toBe('Counsel approved this exact wording on review.')
  })

  it('will not let the officer override without saying why', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/automatic-message-templates/') && init?.method === 'POST') {
        return response(WARNING_409, 409)
      }
      return response(CATALOG)
    })
    const user = await openEditorWith(fetchMock)
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByRole('textbox', { name: /why publish it anyway/i })

    expect(screen.getByRole('button', { name: 'Save draft anyway' })).toBeDisabled()
  })

  it('tells a role that may not override who can', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/automatic-message-templates/') && init?.method === 'POST') {
        return response({
          ...WARNING_409,
          code: 'guardrail_not_overridable',
          may_override: false,
          detail: 'Only a Records Officer or Application Admin may publish one that does not.',
        }, 409)
      }
      return response(CATALOG)
    })
    const user = await openEditorWith(fetchMock)
    await user.click(screen.getByRole('button', { name: 'Save draft' }))

    expect(
      await screen.findByText(/Only a Records Officer or Application Admin/i),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Save draft anyway' }),
    ).not.toBeInTheDocument()
  })
})
