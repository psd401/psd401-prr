// ABOUTME: Keyboard, focus, current-page, and count-badge contracts for the shell.
// ABOUTME: The compact navigation remains operable without a pointer.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { StaffUser } from '../api/auth'
import { AppLayout } from './AppLayout'

const user: StaffUser = {
  authenticated: true,
  username: 'officer',
  email: 'officer@psd401.net',
  first_name: 'Record',
  last_name: 'Officer',
  is_superuser: false,
  roles: ['records_officer'],
}

function response(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(payload),
  } as Response
}

function renderLayout() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/inbox']}>
        <Routes>
          <Route element={<AppLayout user={user} />}>
            <Route path="inbox" element={<h1>Inbox screen</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })))
  vi.stubGlobal('fetch', vi.fn(async () => response({
    personal: 4,
    inbox_needs_attention: 2,
    inbox_delivery_exceptions: 1,
    review: 3,
    team: 5,
  })))
})

afterEach(() => vi.unstubAllGlobals())

it('provides a skip link and keyboard drawer with focus return', async () => {
  const event = userEvent.setup()
  renderLayout()

  expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute(
    'href',
    '#main-content',
  )
  expect(screen.getByRole('main')).toHaveAttribute('tabindex', '-1')

  const trigger = screen.getByRole('button', { name: 'Open navigation' })
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  await event.click(trigger)
  expect(trigger).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()

  await event.keyboard('{Escape}')
  expect(trigger).toHaveFocus()
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
})

it('marks the current page and gives count badges complete accessible names', async () => {
  const event = userEvent.setup()
  renderLayout()
  await event.click(screen.getByRole('button', { name: 'Open navigation' }))

  const inbox = screen.getByRole('link', { name: /Inbox/ })
  expect(inbox).toHaveAttribute('aria-current', 'page')
  await waitFor(() => {
    expect(screen.getByLabelText('2 items in Inbox, needing attention')).toBeInTheDocument()
    expect(screen.getByLabelText('3 items in Review, awaiting review')).toBeInTheDocument()
  })
})

it('moves focus into the destination when a mobile navigation link closes', async () => {
  const event = userEvent.setup()
  renderLayout()
  await event.click(screen.getByRole('button', { name: 'Open navigation' }))

  await event.click(screen.getByRole('link', { name: /Inbox/ }))

  await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
  expect(screen.getByRole('button', { name: 'Open navigation' }))
    .toHaveAttribute('aria-expanded', 'false')
})
