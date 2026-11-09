// ABOUTME: Knowledge page tests: each section lists, creates, edits, and
// ABOUTME: removes through its own staff API, sending normalized bodies.
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeResponse, page, renderPage } from '../test/pageHarness'
import { Knowledge } from './Knowledge'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

type Call = { url: string; method: string; body: unknown }

function stubApi(data: {
  roles?: unknown[]
  senders?: unknown[]
  notes?: unknown[]
  systems?: unknown[]
  failWrite?: boolean
}) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET'
    const body = init.body ? JSON.parse(String(init.body)) : undefined
    calls.push({ url, method, body })
    if (method !== 'GET') {
      if (data.failWrite) return fakeResponse({ ends_on: ['The end date cannot be before the start date.'] }, 400)
      return method === 'DELETE' ? fakeResponse(null, 204) : fakeResponse({ id: 99, ...body }, 201)
    }
    if (url.startsWith('/api/staff/knowledge/role-accounts/')) return fakeResponse(page(data.roles ?? []))
    if (url.startsWith('/api/staff/knowledge/noise-senders/')) return fakeResponse(page(data.senders ?? []))
    if (url.startsWith('/api/staff/knowledge/system-notes/')) return fakeResponse(page(data.notes ?? []))
    return fakeResponse(page(data.systems ?? []))
  }))
  return calls
}

const role = {
  id: 1,
  role: 'Principal, Example Elementary',
  account_email: 'principal.example@psd401.net',
  starts_on: '2024-07-01',
  ends_on: null,
  note: '',
}

describe('Knowledge', () => {
  it('explains each empty section', async () => {
    stubApi({})
    renderPage(<Knowledge />)

    expect(await screen.findByText('No role accounts yet.')).toBeInTheDocument()
    expect(await screen.findByText('No noise senders yet.')).toBeInTheDocument()
    expect(await screen.findByText('No system search notes yet.')).toBeInTheDocument()
  })

  it('adds a role account with an open end date sent as null', async () => {
    const calls = stubApi({})
    renderPage(<Knowledge />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add role account' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/^Role/), { target: { value: 'Director of Human Resources' } })
    fireEvent.change(within(dialog).getByLabelText('Account email'), { target: { value: 'hr.director@psd401.net' } })
    fireEvent.change(within(dialog).getByLabelText('From'), { target: { value: '2025-07-01' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(calls.some((call) => call.method === 'POST')).toBe(true))
    const post = calls.find((call) => call.method === 'POST')
    expect(post?.url).toBe('/api/staff/knowledge/role-accounts/')
    expect(post?.body).toEqual({
      role: 'Director of Human Resources',
      account_email: 'hr.director@psd401.net',
      starts_on: '2025-07-01',
      ends_on: null,
      note: '',
    })
    expect(await screen.findByText('Role account added.')).toBeInTheDocument()
  })

  it('edits and removes a role account, and shows a refused save', async () => {
    const calls = stubApi({ roles: [role], failWrite: true })
    renderPage(<Knowledge />)
    fireEvent.click(await screen.findByText('Principal, Example Elementary'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Until'), { target: { value: '2024-06-30' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(calls.some((call) => call.method === 'PATCH')).toBe(true))
    expect(calls.find((call) => call.method === 'PATCH')?.url).toBe('/api/staff/knowledge/role-accounts/1/')
    expect((await screen.findAllByText(/end date cannot be before/)).length).toBeGreaterThan(0)
  })

  it('removes a role account after confirmation', async () => {
    const calls = stubApi({ roles: [role] })
    renderPage(<Knowledge />)
    const table = await screen.findByRole('table', { name: 'Role accounts' })
    fireEvent.click(within(table).getByRole('button', { name: 'Remove' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(calls.some((call) => call.method === 'DELETE')).toBe(true))
    expect(calls.find((call) => call.method === 'DELETE')?.url).toBe('/api/staff/knowledge/role-accounts/1/')
    expect(await screen.findByText('Role account removed.')).toBeInTheDocument()
  })

  it('adds a noise sender of the chosen kind', async () => {
    const calls = stubApi({})
    renderPage(<Knowledge />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add noise sender' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Kind'), { target: { value: 'list' } })
    fireEvent.change(within(dialog).getByLabelText(/^Sender/), { target: { value: 'all-staff.example.org' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(calls.some((call) => call.method === 'POST')).toBe(true))
    expect(calls.find((call) => call.method === 'POST')).toMatchObject({
      url: '/api/staff/knowledge/noise-senders/',
      body: { kind: 'list', value: 'all-staff.example.org', note: '', active: true },
    })
  })

  it('deactivates and removes a noise sender', async () => {
    const calls = stubApi({
      senders: [{ id: 3, kind: 'domain', value: 'news.example.org', note: '', active: true }],
    })
    renderPage(<Knowledge />)
    fireEvent.click(await screen.findByText('news.example.org'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByLabelText('Active'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((call) => call.method === 'PATCH')).toBe(true))
    expect(calls.find((call) => call.method === 'PATCH')).toMatchObject({
      url: '/api/staff/knowledge/noise-senders/3/',
      body: { active: false },
    })
    expect(await screen.findByText('Noise sender updated.')).toBeInTheDocument()

    const table = screen.getByRole('table', { name: 'Noise senders' })
    fireEvent.click(within(table).getByRole('button', { name: 'Remove' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(calls.some((call) => call.method === 'DELETE')).toBe(true))
  })

  it('offers only systems without notes, and edits notes without moving them', async () => {
    const calls = stubApi({
      notes: [{
        id: 5,
        record_system_id: 1,
        record_system_slug: 'google-workspace',
        record_system_name: 'Google Workspace',
        notes: 'Search the building mailbox first.',
      }],
      systems: [
        { id: 1, name: 'Google Workspace', active: true },
        { id: 2, name: 'School Bus Video', active: true },
        { id: 3, name: 'Retired system', active: false },
      ],
    })
    renderPage(<Knowledge />)
    await screen.findByText('Search the building mailbox first.')
    fireEvent.click(await screen.findByRole('button', { name: 'Add search notes' }))
    let dialog = await screen.findByRole('dialog')
    const options = within(within(dialog).getByLabelText('System')).getAllByRole('option')
    expect(options.map((option) => option.textContent)).toEqual(['School Bus Video'])
    fireEvent.change(within(dialog).getByLabelText('Search notes'), { target: { value: 'Ask transportation for the DVR id.' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((call) => call.method === 'POST')).toBe(true))
    expect(calls.find((call) => call.method === 'POST')).toMatchObject({
      url: '/api/staff/knowledge/system-notes/',
      body: { record_system_id: 2, notes: 'Ask transportation for the DVR id.' },
    })
    expect(await screen.findByText('Search notes added.')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Search the building mailbox first.'))
    dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByLabelText('System')).not.toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText('Search notes'), { target: { value: 'Updated.' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((call) => call.method === 'PATCH')).toBe(true))
    expect(calls.find((call) => call.method === 'PATCH')).toMatchObject({
      url: '/api/staff/knowledge/system-notes/5/',
      body: { notes: 'Updated.' },
    })

    const table = screen.getByRole('table', { name: 'System search notes' })
    fireEvent.click(within(table).getByRole('button', { name: 'Remove' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(calls.some((call) => call.method === 'DELETE')).toBe(true))
    expect(calls.find((call) => call.method === 'DELETE')?.url).toBe('/api/staff/knowledge/system-notes/5/')
  })
})
