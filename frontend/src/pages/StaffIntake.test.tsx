// ABOUTME: Staff intake UI contract for minimal contact, historical receipt
// ABOUTME: time, multipart safety, idempotent retry, and PII-free confirmation.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { StaffIntake } from './StaffIntake'
import { at } from '../test/at'

function response(status: number, payload: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  }
}

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <StaffIntake />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  document.cookie = 'csrftoken=; Max-Age=0; path=/'
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('collects only operational intake facts and does not require email or identity', () => {
  renderPage()

  expect(screen.getByLabelText('How was the request received?')).toBeRequired()
  expect(screen.getByLabelText('Date and time received')).toBeRequired()
  expect(screen.getByLabelText('What records were requested?')).toBeRequired()
  expect(screen.getByLabelText('Email address (optional)')).not.toBeRequired()
  expect(screen.getByLabelText('Phone number (optional)')).not.toBeRequired()
  expect(screen.queryByLabelText(/purpose|reason for request|identity/i)).not.toBeInTheDocument()
  expect(screen.getByText(/at least one response method/i)).toBeInTheDocument()
})

it('opens a public records request unless the clerk says otherwise', async () => {
  document.cookie = 'csrftoken=staff-intake-csrf; path=/'
  const fetchMock = vi.fn(async (
    _input: RequestInfo | URL,
    _options?: RequestInit,
  ) => response(201, { number: 'FER-2026-0001' }))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  const kind = screen.getByLabelText('What kind of matter is this?')
  expect(kind).toHaveValue('public_records')

  // The kind decides the deadline, the letters, and the JLARC count, so it is
  // chosen deliberately here and never again.
  await user.selectOptions(kind, 'ferpa_parent')
  await user.type(
    screen.getByLabelText('What records were requested?'),
    "My child's attendance record.",
  )
  await user.type(screen.getByLabelText('Phone number (optional)'), '253-555-0123')
  await user.click(screen.getByRole('button', { name: 'Log request' }))

  expect(await screen.findByText('FER-2026-0001')).toBeInTheDocument()
  const body = (at(fetchMock.mock.calls, 0)[1] as RequestInit).body as FormData
  expect(body.get('matter_kind')).toBe('ferpa_parent')
})

it('submits multipart data with the original receipt time and an idempotency key', async () => {
  document.cookie = 'csrftoken=staff-intake-csrf; path=/'
  const fetchMock = vi.fn(async (
    _input: RequestInfo | URL,
    _options?: RequestInit,
  ) => response(201, { number: 'PRR-2026-0201' }))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  await user.selectOptions(screen.getByLabelText('How was the request received?'), 'phone')
  await user.clear(screen.getByLabelText('Date and time received'))
  await user.type(screen.getByLabelText('Date and time received'), '2026-08-02T14:30')
  await user.type(screen.getByLabelText('What records were requested?'), 'Bus camera footage.')
  await user.type(screen.getByLabelText('Phone number (optional)'), '253-555-0123')
  await user.upload(
    screen.getByLabelText('Scanned request or supporting files (optional)'),
    new File(['paper request'], 'request.txt', { type: 'text/plain' }),
  )
  await user.click(screen.getByRole('button', { name: 'Log request' }))

  expect(await screen.findByText('PRR-2026-0201')).toBeInTheDocument()
  expect(screen.getByText(/request has entered the compliance queue/i)).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  const [path, options] = at(fetchMock.mock.calls, 0)
  const requestOptions = options as RequestInit
  expect(path).toBe('/api/staff/requests/intake/')
  expect(options).toMatchObject({
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'X-CSRFToken': 'staff-intake-csrf',
    },
  })
  expect((requestOptions.headers as Record<string, string>)['Content-Type']).toBeUndefined()
  const body = requestOptions.body as FormData
  expect(body).toBeInstanceOf(FormData)
  expect(body.get('channel')).toBe('phone')
  expect(body.get('received_at')).toBe(new Date('2026-08-02T14:30').toISOString())
  expect(body.get('description')).toBe('Bus camera footage.')
  expect(body.get('phone')).toBe('253-555-0123')
  expect(body.get('idempotency_key')).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  )
  expect(body.get('attachments')).toMatchObject({ name: 'request.txt' })
})

it('keeps the same idempotency key when a failed submission is retried', async () => {
  document.cookie = 'csrftoken=staff-intake-csrf; path=/'
  let attempt = 0
  const fetchMock = vi.fn(async (
    _input: RequestInfo | URL,
    _options?: RequestInit,
  ) => {
    attempt += 1
    if (attempt === 1) throw new Error('Connection reset')
    return response(201, { number: 'PRR-2026-0202' })
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  await user.type(screen.getByLabelText('What records were requested?'), 'Board minutes.')
  await user.type(screen.getByLabelText('Email address (optional)'), 'caller@example.org')
  await user.click(screen.getByRole('button', { name: 'Log request' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Connection reset')

  await user.click(screen.getByRole('button', { name: 'Log request' }))
  expect(await screen.findByText('PRR-2026-0202')).toBeInTheDocument()

  const first = at(fetchMock.mock.calls, 0)[1]?.body as FormData
  const second = at(fetchMock.mock.calls, 1)[1]?.body as FormData
  expect(second.get('idempotency_key')).toBe(first.get('idempotency_key'))
})

it('shows the retained request number when downstream intake processing fails', async () => {
  const fetchMock = vi.fn(async (
    _input: RequestInfo | URL,
    _options?: RequestInit,
  ) => response(503, {
    detail: 'The submission was accepted but processing did not complete.',
    number: 'PRR-2026-0203',
  }))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  await user.type(screen.getByLabelText('What records were requested?'), 'Contracts.')
  await user.type(screen.getByLabelText('Email address (optional)'), 'caller@example.org')
  await user.click(screen.getByRole('button', { name: 'Log request' }))

  expect(await screen.findByText('PRR-2026-0203')).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent(/do not submit it again/i)
  expect(screen.queryByRole('button', { name: 'Log request' })).not.toBeInTheDocument()
})

it('requires a response method before sending any PII', async () => {
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage()

  await user.type(screen.getByLabelText('What records were requested?'), 'Budget documents.')
  await user.click(screen.getByRole('button', { name: 'Log request' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(/at least one response method/i)
  expect(fetchMock).not.toHaveBeenCalled()
})
