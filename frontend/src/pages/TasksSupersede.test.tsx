// ABOUTME: A Records Officer supersedes a completed Vault collection with a
// ABOUTME: stated reason; liaisons and unfinished collections never see it.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import type { StaffRole } from '../api/auth'
import { ToastProvider } from '../ui'
import { Tasks } from './Tasks'
import { at } from '../test/at'

const REASON = 'The first scope swept in district mass mail.'

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as Response
}

function vaultTask(state: string) {
  return {
    id: 17,
    request: '11111111-1111-4111-8111-111111111111',
    request_number: 'PRR-2026-0017',
    system_name: null,
    system_slug: null,
    system_urgent_hold: null,
    kind: 'vault_mail',
    state,
    instructions: 'Create the confirmed Google Vault Gmail export.',
    custodian_emails: [],
    search_terms: '"board minutes"',
    date_range_start: null,
    date_range_end: null,
    assigned_to_id: null,
    assigned_to_username: null,
    due_date: null,
    vault_matter_id: '',
    vault_hold_id: '',
    vault_export_id: '',
    vault_ingest: null,
    created_at: '2026-08-01T12:00:00Z',
    completed_at: null,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function openTask(task: ReturnType<typeof vaultTask>, roles: StaffRole[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/supersede/')) {
      expect(init?.method).toBe('POST')
      expect(JSON.parse(String(init?.body))).toEqual({ reason: REASON })
      return response({ collection_task_id: task.id, record_count: 40 })
    }
    if (url.includes('/staff-users/')) return response({ results: [] })
    if (url.includes(`/collection-tasks/${task.id}/`)) return response(task)
    return response({ results: [task] })
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const user = userEvent.setup()
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter><Tasks roles={roles} /></MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
  await user.click(at(await screen.findAllByRole('row'), 1))
  await screen.findByRole('dialog')
  return { user, fetchMock }
}

it('supersedes a completed collection only after a substantive reason', async () => {
  const { user, fetchMock } = await openTask(vaultTask('complete'), ['records_officer'])

  await user.click(screen.getByRole('button', { name: 'Supersede collection' }))
  const confirm = screen.getByRole('button', { name: 'Supersede unreviewed records' })
  expect(confirm).toBeDisabled()

  await user.type(screen.getByLabelText('Reason'), REASON)
  expect(confirm).toBeEnabled()
  await user.click(confirm)

  expect(await screen.findByText('40 unreviewed records left review.')).toBeInTheDocument()
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/supersede/'))).toBe(true)
})

it('offers no supersede control to a liaison', async () => {
  await openTask(vaultTask('complete'), ['liaison'])
  expect(screen.queryByRole('button', { name: 'Supersede collection' })).not.toBeInTheDocument()
})

it('offers no supersede control before the collection completes', async () => {
  await openTask(vaultTask('ingesting'), ['records_officer'])
  expect(screen.queryByRole('button', { name: 'Supersede collection' })).not.toBeInTheDocument()
})
