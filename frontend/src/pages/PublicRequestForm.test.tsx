// ABOUTME: Public request form acceptance tests for anonymous routing, secure
// ABOUTME: multipart submission, recoverable failures, and accessible errors.
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { PublicRequestForm } from './PublicRequestForm'
import { PublicRequestReceipt } from './PublicRequestReceipt'
import { at } from '../test/at'

function response(status: number, payload: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  }
}

function renderPublicForm() {
  return render(
    <MemoryRouter initialEntries={['/request']}>
      <Routes>
        <Route path="/request" element={<PublicRequestForm />} />
        <Route path="/request/submitted" element={<PublicRequestReceipt />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  document.cookie = 'csrftoken=; Max-Age=0; path=/'
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: undefined,
  })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('requires only the request description and email and labels every optional field', () => {
  renderPublicForm()

  expect(screen.getByLabelText('What records are you requesting?')).toBeRequired()
  expect(screen.getByLabelText('Email address')).toBeRequired()
  for (const label of [
    'Full name (optional)',
    'Phone number (optional)',
    'Mailing address (optional)',
    'City (optional)',
    'State (optional)',
    'Postal code (optional)',
    'Company or organization (optional)',
    'Attachments (optional)',
  ]) {
    expect(screen.getByLabelText(label)).toBeInTheDocument()
  }
  expect(screen.queryByLabelText(/purpose|reason|identity/i)).not.toBeInTheDocument()
  expect(screen.getByText(/do not need to explain why/i)).toBeInTheDocument()
  expect(screen.getByText(/within five business days/i)).toBeInTheDocument()
})

it('submits multipart data with CSRF and routes to a copyable request number', async () => {
  document.cookie = 'csrftoken=public-csrf-token; path=/'
  const fetchMock = vi.fn(async (
    _input: RequestInfo | URL,
    _options?: RequestInit,
  ) => response(201, { number: 'PRR-2026-0123' }))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPublicForm()

  await user.type(
    screen.getByLabelText('What records are you requesting?'),
    'Emails about the 2026 transportation plan.',
  )
  await user.type(screen.getByLabelText('Email address'), 'jamie@example.org')
  await user.type(screen.getByLabelText('Full name (optional)'), 'Jamie Requester')
  await user.upload(
    screen.getByLabelText('Attachments (optional)'),
    new File(['supporting context'], 'context.txt', { type: 'text/plain' }),
  )
  await user.click(screen.getByRole('button', { name: 'Submit request' }))

  expect(await screen.findByText('PRR-2026-0123')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Request submitted' })).toBeInTheDocument()
  expect(screen.getByText(/confirmation email/i)).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  const [path, options] = at(fetchMock.mock.calls, 0)
  expect(path).toBe('/api/public/requests/')
  expect(options).toMatchObject({
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'X-CSRFToken': 'public-csrf-token',
    },
  })
  const body = options?.body
  expect(body).toBeInstanceOf(FormData)
  expect((body as FormData).get('description')).toBe(
    'Emails about the 2026 transportation plan.',
  )
  expect((body as FormData).get('email')).toBe('jamie@example.org')
  expect((body as FormData).get('name')).toBe('Jamie Requester')
  expect((body as FormData).get('website')).toBe('')
  expect((body as FormData).get('attachments')).toMatchObject({ name: 'context.txt' })
})

it('preserves the complete form after throttling and provides a fallback contact', async () => {
  document.cookie = 'csrftoken=public-csrf-token; path=/'
  vi.stubGlobal('fetch', vi.fn(async () => response(429, { detail: 'Request was throttled.' })))
  const user = userEvent.setup()
  renderPublicForm()
  const description = 'A long request description that must survive a failed submission.'

  await user.type(screen.getByLabelText('What records are you requesting?'), description)
  await user.type(screen.getByLabelText('Email address'), 'jamie@example.org')
  await user.type(screen.getByLabelText('City (optional)'), 'Gig Harbor')
  await user.click(screen.getByRole('button', { name: 'Submit request' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(/too many submissions/i)
  expect(screen.getByLabelText('What records are you requesting?')).toHaveValue(description)
  expect(screen.getByLabelText('Email address')).toHaveValue('jamie@example.org')
  expect(screen.getByLabelText('City (optional)')).toHaveValue('Gig Harbor')
  expect(screen.getByRole('link', { name: 'publicrecords@psd401.net' })).toHaveAttribute(
    'href',
    'mailto:publicrecords@psd401.net',
  )
})

it('announces server field errors and associates them with their input', async () => {
  document.cookie = 'csrftoken=public-csrf-token; path=/'
  vi.stubGlobal('fetch', vi.fn(async () => response(400, {
    email: ['Enter a valid email address.'],
  })))
  const user = userEvent.setup()
  renderPublicForm()

  await user.type(screen.getByLabelText('What records are you requesting?'), 'Board emails.')
  await user.type(screen.getByLabelText('Email address'), 'valid@example.org')
  await user.click(screen.getByRole('button', { name: 'Submit request' }))

  const error = await screen.findByText('Enter a valid email address.')
  expect(error).toHaveAttribute('role', 'alert')
  expect(screen.getByLabelText('Email address')).toHaveAttribute(
    'aria-describedby',
    expect.stringContaining(error.id),
  )
})

it('rejects attachment selections that exceed the server count limit before upload', async () => {
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPublicForm()
  const input = screen.getByLabelText('Attachments (optional)')
  const files = Array.from(
    { length: 11 },
    (_, index) => new File(['context'], `context-${index}.txt`, { type: 'text/plain' }),
  )

  await user.upload(input, files)

  expect(await screen.findByRole('alert')).toHaveTextContent(/maximum of 10 files/i)
  expect(screen.getByRole('list', { name: 'Selected attachments' })).toHaveTextContent(
    'context-0.txt',
  )
  expect((screen.getByLabelText('Attachments (optional)') as HTMLInputElement).files).toHaveLength(11)
  expect(screen.getByText(/25 MB each and 50 MB total/i)).toBeInTheDocument()
  await user.type(screen.getByLabelText('What records are you requesting?'), 'Board emails.')
  await user.type(screen.getByLabelText('Email address'), 'jamie@example.org')
  await user.click(screen.getByRole('button', { name: 'Submit request' }))
  expect(screen.getByRole('alert')).toHaveTextContent(/maximum of 10 files/i)
  expect(fetchMock).not.toHaveBeenCalled()
})

it('lets the server validate an accepted attachment when the browser omits its MIME type', async () => {
  document.cookie = 'csrftoken=public-csrf-token; path=/'
  const fetchMock = vi.fn(async (
    _input: RequestInfo | URL,
    _options?: RequestInit,
  ) => response(201, { number: 'PRR-2026-0124' }))
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPublicForm()

  await user.type(screen.getByLabelText('What records are you requesting?'), 'An attached email.')
  await user.type(screen.getByLabelText('Email address'), 'jamie@example.org')
  await user.upload(
    screen.getByLabelText('Attachments (optional)'),
    new File(['From: sender@example.org'], 'message.eml', { type: '' }),
  )
  await user.click(screen.getByRole('button', { name: 'Submit request' }))

  expect(await screen.findByText('PRR-2026-0124')).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  const submittedBody = at(fetchMock.mock.calls, 0)[1]?.body as FormData
  expect(submittedBody.get('attachments')).toMatchObject({
    name: 'message.eml',
    type: 'message/rfc822',
  })
})

it('shows manual-copy guidance when clipboard access is unavailable', async () => {
  const user = userEvent.setup()
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: undefined,
  })

  render(
    <MemoryRouter initialEntries={['/request/submitted?number=PRR-2026-0125']}>
      <PublicRequestReceipt />
    </MemoryRouter>,
  )
  await user.click(screen.getByRole('button', { name: 'Copy request number' }))

  expect(await screen.findByRole('status')).toHaveTextContent(/select the request number/i)
})

it('handles a rejected clipboard write without an unhandled error', async () => {
  const user = userEvent.setup()
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: vi.fn(async () => { throw new Error('Clipboard denied') }) },
  })

  render(
    <MemoryRouter initialEntries={['/request/submitted?number=PRR-2026-0126']}>
      <PublicRequestReceipt />
    </MemoryRouter>,
  )
  await user.click(screen.getByRole('button', { name: 'Copy request number' }))

  expect(await screen.findByRole('status')).toHaveTextContent(/select the request number/i)
  expect(screen.getByRole('button', { name: 'Copy request number' })).toBeInTheDocument()
})
