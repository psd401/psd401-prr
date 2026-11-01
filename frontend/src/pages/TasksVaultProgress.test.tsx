// ABOUTME: Vault collection-task progress stays visible without exposing
// ABOUTME: backend-owned task transitions or storage/external-object evidence.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { Tasks } from './Tasks'
import { at } from '../test/at'

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as Response
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('shows safe Vault ingest progress and keeps its state read-only', async () => {
  const vaultTask = {
    id: 17,
    request: '11111111-1111-4111-8111-111111111111',
    request_number: 'PRR-2026-0017',
    system_name: 'Google Workspace',
    system_slug: 'google-workspace',
    system_urgent_hold: false,
    kind: 'vault_mail',
    state: 'ingesting',
    instructions: 'Retain and ingest a completed Vault export.',
    custodian_emails: ['records@example.org'],
    search_terms: 'board minutes',
    date_range_start: null,
    date_range_end: null,
    assigned_to_id: null,
    assigned_to_username: null,
    due_date: null,
    vault_matter_id: 'matter-hidden',
    vault_hold_id: 'hold-hidden',
    vault_export_id: 'export-hidden',
    vault_ingest: {
      state: 'failed',
      attempt_count: 2,
      files_total: 3,
      files_processed: 2,
      messages_seen: 27,
      items_created: 20,
      duplicates_skipped: 7,
      last_error_code: 'message_conversion_failed',
      started_at: '2026-08-01T12:00:00Z',
      updated_at: '2026-08-01T12:30:00Z',
      completed_at: null,
    },
    created_at: '2026-08-01T12:00:00Z',
    completed_at: null,
  }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/staff-users/')) return response({ results: [] })
    // Selecting a task now always records it in the URL, so the detail
    // endpoint is fetched to rehydrate the selection.
    if (url.includes(`/collection-tasks/${vaultTask.id}/`)) return response(vaultTask)
    return response({ results: [vaultTask] })
  }))
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const user = userEvent.setup()

  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter><Tasks roles={['records_officer']} /></MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )

  const row = at(await screen.findAllByRole('row'), 1)
  expect(within(row).getByText('Ingesting export')).toBeInTheDocument()
  expect(within(row).queryByRole('combobox')).not.toBeInTheDocument()

  await user.click(row)

  expect(await screen.findByText('Vault ingest progress')).toBeInTheDocument()
  expect(screen.getByText('2 of 3 files processed')).toBeInTheDocument()
  expect(screen.getByText('27 messages inspected')).toBeInTheDocument()
  expect(screen.getByText('20 review items created')).toBeInTheDocument()
  expect(screen.getByText('7 duplicates retained as evidence')).toBeInTheDocument()
  expect(screen.getByText('Attempt 2 · message_conversion_failed')).toBeInTheDocument()
  expect(screen.queryByText(/matter-hidden|hold-hidden|export-hidden/)).not.toBeInTheDocument()
})
