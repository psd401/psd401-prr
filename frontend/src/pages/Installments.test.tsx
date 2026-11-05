// ABOUTME: Installment production requires an explicit Records Officer
// ABOUTME: confirmation and uses the same registered REST primitive as agents.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Installments } from './Installments'
import { at } from '../test/at'

function response(payload: unknown) {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(payload),
  }
}

const row = {
  id: 7,
  request_number: 'PRR-2026-0042',
  sequence: 1,
  item_count: 3,
  fee_amount: '0.00',
  fee_paid_at: null,
  released_at: null,
  download_expires_at: null,
  package_prepared_at: null,
  package_prepared_by_username: null,
  package_size: null,
  has_exemptions: false,
  exemption_log_ready: false,
  exemption_log_prepared_at: null,
  exemption_log_prepared_by_username: null,
  download_count: 0,
  last_downloaded_at: null,
  requires_access_code: false,
  access_code_set_at: null,
  revoked_at: null,
  revoked_by_username: null,
  revocation_reason: '',
  record_item_ids: [],
  contents_locked: false,
}

const readyRecord = {
  id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  original_filename: 'board-minutes.pdf',
  custodian: 'superintendent@psd401.net',
  review_state: 'responsive',
  content_length: 2048,
  redaction_count: 0,
}

/** Split the two list endpoints the Releases tab reads. */
function listFor(input: RequestInfo | URL, installments: unknown[]) {
  const url = String(input)
  if (url.startsWith('/api/staff/record-items/')) {
    return response({
      count: 1, next: null, previous: null, results: [readyRecord],
    })
  }
  if (
    url.startsWith('/api/staff/fee-schedules/')
    || url.startsWith('/api/staff/fee-waiver-floors/')
    || url.startsWith('/api/staff/fee-deposit-gates/')
  ) {
    return response({ count: 0, next: null, previous: null, results: [] })
  }
  return response({
    count: installments.length,
    next: null,
    previous: null,
    results: installments,
  })
}

/** The first data row of the installments table, which renders last. */
async function installmentRow() {
  const tables = await screen.findAllByRole('table')
  return at(within(at(tables, tables.length - 1)).getAllByRole('row'), 1)
}

function renderPage(
  roles: Array<'records_officer' | 'reviewer'>,
  requestNumber?: string,
) {
  const queryClient = new QueryClient()
  return {
    ...render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <Installments roles={roles} requestNumber={requestNumber} />
      </QueryClientProvider>
    </MemoryRouter>,
    ),
    queryClient,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('requires officer confirmation before preparing an immutable package', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/installments/7/prepare/') {
      return response({
        ...row,
        package_prepared_at: '2026-08-02T19:30:00Z',
        package_prepared_by_username: 'officer',
        package_size: 4096,
      })
    }
    return listFor(input, [row])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  const { queryClient } = renderPage(
    ['records_officer'],
    'PRR-2026-0042',
  )
  queryClient.setQueryData(
    ['case-closure-readiness', 'PRR-2026-0042'],
    { ready: false },
  )

  const requestRow = await installmentRow()
  await user.click(within(requestRow).getByRole('button', { name: 'Prepare package' }))

  const dialog = screen.getByRole('dialog', { name: 'Prepare installment package' })
  expect(within(dialog).getByText(/verify every source against its sha-256/i)).toBeInTheDocument()
  expect(within(dialog).getByText(/cannot be overwritten/i)).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Prepare package' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/prepare/',
    expect.objectContaining({ method: 'POST' }),
  )
  expect(
    queryClient.getQueryState([
      'case-closure-readiness',
      'PRR-2026-0042',
    ])?.isInvalidated,
  ).toBe(true)
})

it('does not offer package preparation to a reviewer', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
    listFor(input, [row])
  )))

  renderPage(['reviewer'])

  expect(await screen.findByText('Installments and releases')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Prepare package' })).not.toBeInTheDocument()
})

it('requires explicit exemption-log generation before packaging exempt records', async () => {
  const exemptRow = { ...row, has_exemptions: true }
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/installments/7/generate-exemption-log/') {
      return response({
        ...exemptRow,
        exemption_log_ready: true,
        exemption_log_prepared_at: '2026-08-02T19:00:00Z',
        exemption_log_prepared_by_username: 'officer',
      })
    }
    return listFor(input, [exemptRow])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  const { queryClient } = renderPage(
    ['records_officer'],
    'PRR-2026-0042',
  )
  queryClient.setQueryData(
    ['case-closure-readiness', 'PRR-2026-0042'],
    { ready: false },
  )

  const requestRow = await installmentRow()
  expect(within(requestRow).queryByRole('button', { name: 'Prepare package' })).not.toBeInTheDocument()
  await user.click(within(requestRow).getByRole('button', { name: 'Generate exemption log' }))
  const dialog = screen.getByRole('dialog', { name: 'Generate exemption log' })
  expect(within(dialog).getByText(/rcw 42\.56\.210\(3\)/i)).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Generate exemption log' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/generate-exemption-log/',
    expect.objectContaining({ method: 'POST' }),
  )
})

it('releases and sends the notice in one confirmed officer action', async () => {
  const prepared = {
    ...row,
    package_prepared_at: '2026-08-02T19:30:00Z',
    package_prepared_by_username: 'officer',
    package_size: 4096,
  }
  const downloadUrl = (
    'https://prr.psd401.net/requests/PRR-2026-0042/'
    + 'installments/1/download/opaque-token/'
  )
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/installments/7/release-and-send/') {
      return response({
        ...prepared,
        released_at: '2026-08-02T20:00:00Z',
        download_expires_at: '2026-08-09T20:00:00Z',
        download_url: downloadUrl,
        correspondence_id: '11111111-1111-4111-8111-111111111111',
        correspondence_status: 'queued',
      })
    }
    return listFor(input, [prepared])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  const { queryClient } = renderPage(
    ['records_officer'],
    'PRR-2026-0042',
  )
  queryClient.setQueryData(
    ['case-closure-readiness', 'PRR-2026-0042'],
    { ready: false },
  )

  const requestRow = await installmentRow()
  await user.click(within(requestRow).getByRole('button', { name: 'Release and send' }))
  const dialog = screen.getByRole('dialog', { name: 'Release installment' })
  expect(within(dialog).getByText(/link will expire/i)).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Release and send' }))

  expect(await screen.findByDisplayValue(downloadUrl)).toBeInTheDocument()
  expect(screen.getByText(/notice has been sent/i)).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/release-and-send/',
    expect.objectContaining({ method: 'POST' }),
  )
  expect(
    queryClient.getQueryState([
      'case-closure-readiness',
      'PRR-2026-0042',
    ])?.isInvalidated,
  ).toBe(true)
})

it('activates a counsel-approved adopted schedule before fees can be assessed', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input) === '/api/staff/fee-schedules/' && options?.method === 'POST') {
      return response({
        id: '11111111-1111-4111-8111-111111111111',
        name: 'PSD adopted PRA copy schedule',
        basis: 'statutory_default',
        effective_on: '2026-08-01',
        counsel_approval_reference: 'Legal memo PRA-fees-2026-07-28',
        adoption_reference: 'Board policy 4040 resolution 2026-08-01',
        photocopy_page_rate: '0.1500',
        scanned_page_rate: '0.1000',
        electronic_file_bundle_rate: '0.0500',
        electronic_gb_rate: '0.1000',
        flat_fee_amount: '2.00',
        is_current: true,
      })
    }
    if (String(input) === '/api/staff/fee-schedules/') {
      return response({ count: 0, next: null, previous: null, results: [] })
    }
    return listFor(input, [row])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'])

  await user.click(await screen.findByRole('button', { name: 'Activate fee schedule' }))
  const dialog = screen.getByRole('dialog', { name: 'Activate counsel-approved fee schedule' })
  await user.type(within(dialog).getByLabelText('Schedule name'), 'PSD adopted PRA copy schedule')
  await user.type(within(dialog).getByLabelText('Effective date'), '2026-08-01')
  await user.type(within(dialog).getByLabelText('Counsel approval reference'), 'Legal memo PRA-fees-2026-07-28')
  await user.type(within(dialog).getByLabelText('Formal adoption reference'), 'Board policy 4040 resolution 2026-08-01')
  await user.click(within(dialog).getByRole('button', { name: 'Activate schedule' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/fee-schedules/',
    expect.objectContaining({ method: 'POST' }),
  )
  const call = fetchMock.mock.calls.find(([input, options]) => (
    String(input) === '/api/staff/fee-schedules/' && options?.method === 'POST'
  ))!
  expect(JSON.parse(String(call[1]?.body))).toEqual(expect.objectContaining({
    counsel_approval_reference: 'Legal memo PRA-fees-2026-07-28',
    adoption_reference: 'Board policy 4040 resolution 2026-08-01',
  }))
})

it('assesses an installment and records payment through the adopted schedule', async () => {
  const schedule = {
    id: '11111111-1111-4111-8111-111111111111',
    name: 'PSD adopted PRA copy schedule',
    basis: 'statutory_default',
    effective_on: '2026-08-01',
    counsel_approval_reference: 'Legal memo PRA-fees-2026-07-28',
    adoption_reference: 'Board policy 4040 resolution 2026-08-01',
    photocopy_page_rate: '0.1500',
    scanned_page_rate: '0.1000',
    electronic_file_bundle_rate: '0.0500',
    electronic_gb_rate: '0.1000',
    flat_fee_amount: '2.00',
    is_current: true,
  }
  const assessed = {
    ...row,
    fee_amount: '0.10',
    fee_paid_amount: '0.00',
    fee_balance: '0.10',
    fee_assessment: {
      id: '22222222-2222-4222-8222-222222222222',
      revision: 1,
      amount: '0.10',
      deposit_amount: '0.01',
      reason: 'Electronic delivery under the adopted district fee schedule.',
    },
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/fee-schedules/') {
      return response({ count: 1, next: null, previous: null, results: [schedule] })
    }
    if (String(input) === '/api/staff/installments/7/assess-fee/') return response(assessed)
    if (String(input) === '/api/staff/installments/7/record-payment/') {
      return response({
        ...assessed,
        fee_paid_at: '2026-08-02T20:00:00Z',
        fee_paid_amount: '0.10',
        fee_balance: '0.00',
      })
    }
    return response({ count: 1, next: null, previous: null, results: [assessed] })
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'])

  const requestRow = await installmentRow()
  await user.click(within(requestRow).getByRole('button', { name: 'Assess fee' }))
  const assessmentDialog = screen.getByRole('dialog', { name: 'Assess installment fee' })
  await user.clear(within(assessmentDialog).getByLabelText('Electronic files'))
  await user.type(within(assessmentDialog).getByLabelText('Electronic files'), '8')
  await user.type(
    within(assessmentDialog).getByLabelText('Assessment reason'),
    'Electronic delivery under the adopted district fee schedule.',
  )
  await user.click(within(assessmentDialog).getByRole('button', { name: 'Save assessment' }))
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/assess-fee/',
    expect.objectContaining({ method: 'POST' }),
  )

  await user.click(within(requestRow).getByRole('button', { name: 'Record payment' }))
  const paymentDialog = screen.getByRole('dialog', { name: 'Record fee payment' })
  await user.type(within(paymentDialog).getByLabelText('Receipt reference'), 'District receipt 1042')
  await user.click(within(paymentDialog).getByRole('button', { name: 'Record payment' }))
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/record-payment/',
    expect.objectContaining({ method: 'POST' }),
  )
})

it('creates an installment from the ready-to-release list without retyping anything', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input) === '/api/staff/installments/' && options?.method === 'POST') {
      return response({ ...row, id: 8, sequence: 2, item_count: 1 })
    }
    return listFor(input, [row])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  await user.click(await screen.findByRole('checkbox', { name: /select all/i }))
  await user.click(screen.getByRole('button', { name: 'Create installment (1)' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        request_number: 'PRR-2026-0042',
        record_item_ids: [readyRecord.id],
      }),
    }),
  )
})

it('asks the ready list only for release-ready, unclaimed records', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => listFor(input, [row]))
  vi.stubGlobal('fetch', fetchMock)
  renderPage(['records_officer'], 'PRR-2026-0042')

  await screen.findByText('board-minutes.pdf')

  const requested = fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.startsWith('/api/staff/record-items/'))
  expect(requested.some((url) => (
    url.includes('release_ready=true') && url.includes('unreleased=true')
  ))).toBe(true)
})

it('revokes a released download only with a substantive reason', async () => {
  const released = {
    ...row,
    package_prepared_at: '2026-08-02T19:30:00Z',
    released_at: '2026-08-02T20:00:00Z',
    download_expires_at: '2026-08-09T20:00:00Z',
    download_count: 2,
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input) === '/api/staff/installments/7/revoke/' && options?.method === 'POST') {
      return response({ ...released, revoked_at: '2026-08-03T10:00:00Z' })
    }
    return listFor(input, [released])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  const requestRow = await installmentRow()
  await user.click(within(requestRow).getByRole('button', { name: 'Revoke download' }))
  const dialog = screen.getByRole('dialog', { name: 'Revoke requester download' })
  expect(within(dialog).getByRole('button', { name: 'Revoke download' })).toBeDisabled()

  await user.type(
    within(dialog).getByLabelText('Reason for revoking'),
    'The wrong custodian mailbox was packaged.',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Revoke download' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/revoke/',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        reason: 'The wrong custodian mailbox was packaged.',
      }),
    }),
  )
})

it('sets an access code on an installment and never shows one back', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (
      String(input) === '/api/staff/installments/7/set-access-code/'
      && options?.method === 'POST'
    ) {
      return response({ ...row, requires_access_code: true })
    }
    return listFor(input, [row])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  const requestRow = await installmentRow()
  await user.click(within(requestRow).getByRole('button', { name: 'Add access code' }))
  const dialog = screen.getByRole('dialog', { name: 'Installment access code' })
  expect(within(dialog).getByText(/separate message/i)).toBeInTheDocument()
  await user.type(within(dialog).getByLabelText('Access code'), 'salmon-bay-42')
  await user.click(within(dialog).getByRole('button', { name: 'Set access code' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/set-access-code/',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ access_code: 'salmon-bay-42' }),
    }),
  )
})

it('re-scopes an open installment through set-items', async () => {
  const open = { ...row, record_item_ids: [readyRecord.id] }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input) === '/api/staff/installments/7/set-items/' && options?.method === 'POST') {
      return response({ ...open, item_count: 1 })
    }
    return listFor(input, [open])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  const requestRow = await installmentRow()
  await user.click(within(requestRow).getByRole('button', { name: 'Edit contents' }))
  const dialog = screen.getByRole('dialog', { name: 'Edit installment contents' })
  expect(within(dialog).getByText(/immutable legal artifact/i)).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Save 1 records' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/installments/7/set-items/',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ record_item_ids: [readyRecord.id] }),
    }),
  )
})

it('locks contents and hides the edit action once a package is prepared', async () => {
  const locked = {
    ...row,
    package_prepared_at: '2026-08-02T19:30:00Z',
    contents_locked: true,
  }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => listFor(input, [locked])))
  renderPage(['records_officer'], 'PRR-2026-0042')

  const requestRow = await installmentRow()
  expect(
    within(requestRow).queryByRole('button', { name: 'Edit contents' }),
  ).not.toBeInTheDocument()
})

it('shows a release that stands when its notice did not send, and refreshes the row', async () => {
  // The server answers 409 rather than 400 here because the release committed
  // and only the send failed. The officer must read that the link is live and
  // the notice is still a draft, and the row must stop offering "Release and
  // send" as though nothing had happened.
  const prepared = {
    ...row,
    package_prepared_at: '2026-08-02T19:30:00Z',
    package_prepared_by_username: 'officer',
    package_size: 4096,
    contents_locked: true,
  }
  let released = false
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/staff/installments/7/release-and-send/') {
      released = true
      return {
        ok: false,
        status: 409,
        text: async () => JSON.stringify({
          ...prepared,
          released_at: '2026-08-02T20:00:00Z',
          code: 'installment_notice_unsent',
          detail: 'The package was released and its notice drafted, but the '
            + 'notice was not sent: Outbound correspondence transport is disabled.',
        }),
      }
    }
    return listFor(input, [
      released ? { ...prepared, released_at: '2026-08-02T20:00:00Z' } : prepared,
    ])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  const requestRow = await installmentRow()
  await user.click(within(requestRow).getByRole('button', { name: 'Release and send' }))
  const dialog = screen.getByRole('dialog', { name: 'Release installment' })
  await user.click(within(dialog).getByRole('button', { name: 'Release and send' }))

  expect(await within(dialog).findByText(/was not sent/)).toBeInTheDocument()
  // The list was re-read after the failure and now shows the row released.
  const refreshedRow = await installmentRow()
  expect(within(refreshedRow).queryByRole('button', { name: 'Release and send' }))
    .not.toBeInTheDocument()
  expect(within(refreshedRow).getByRole('button', { name: 'Revoke download' }))
    .toBeInTheDocument()
})

const adoptedSchedule = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'PSD adopted PRA copy schedule',
  basis: 'statutory_default',
  effective_on: '2026-08-01',
  counsel_approval_reference: 'Legal memo PRA-fees-2026-07-28',
  adoption_reference: 'Board policy 4040 resolution 2026-08-01',
  photocopy_page_rate: '0.1500',
  scanned_page_rate: '0.1000',
  electronic_file_bundle_rate: '0.0500',
  electronic_gb_rate: '0.1000',
  flat_fee_amount: '2.00',
  is_current: true,
}

const adoptedFloor = {
  id: '33333333-3333-4333-8333-333333333333',
  amount: '1.00',
  counsel_reference: 'Legal memo PRA-waiver-floor-2026-09-21',
  effective_on: '2026-08-01',
  is_current: true,
}

const openProductionGate = {
  id: '44444444-4444-4444-8444-444444444444',
  request_number: 'PRR-2026-0042',
  scope: 'production' as const,
  amount: '36.72',
  estimated_total: '367.25',
  reason: 'Ten percent deposit on the estimated copying charge.',
  requested_on: '2026-08-05',
  satisfied_at: null,
  payment_amount: null,
  payment_reference: '',
  lifted_at: null,
  lift_reason: '',
  is_open: true,
}

it('adopts a waiver floor with a counsel reference and waives nothing itself', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url === '/api/staff/fee-waiver-floors/' && options?.method === 'POST') {
      return response(adoptedFloor)
    }
    return listFor(input, [row])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'])

  await user.click(
    await screen.findByRole('button', { name: 'Adopt waiver floor' }),
  )
  const dialog = screen.getByRole('dialog', {
    name: 'Adopt the fee waiver floor',
  })
  expect(within(dialog).getByText(/waives nothing by itself/i)).toBeInTheDocument()
  await user.type(
    within(dialog).getByLabelText('Counsel reference'),
    'Legal memo PRA-waiver-floor-2026-09-21',
  )
  await user.type(within(dialog).getByLabelText('Effective date'), '2026-08-01')
  await user.click(within(dialog).getByRole('button', { name: 'Adopt floor' }))

  const call = fetchMock.mock.calls.find(([input, options]) => (
    String(input) === '/api/staff/fee-waiver-floors/'
    && options?.method === 'POST'
  ))!
  expect(JSON.parse(String(call[1]?.body))).toEqual({
    amount: '1.00',
    counsel_reference: 'Legal memo PRA-waiver-floor-2026-09-21',
    effective_on: '2026-08-01',
  })
})

it('prompts the officer when an assessment falls under the adopted floor', async () => {
  const belowFloor = {
    ...row,
    fee_amount: '0.30',
    fee_paid_amount: '0.00',
    fee_balance: '0.30',
    fee_assessment: {
      id: '22222222-2222-4222-8222-222222222222',
      revision: 1,
      amount: '0.30',
      deposit_amount: '0.00',
      reason: 'Three scanned pages under the adopted schedule.',
      waiver_floor_amount: '1.00',
      below_waiver_floor: true,
    },
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/fee-schedules/')) {
      return response({
        count: 1, next: null, previous: null, results: [adoptedSchedule],
      })
    }
    if (url.startsWith('/api/staff/fee-waiver-floors/')) {
      return response({
        count: 1, next: null, previous: null, results: [adoptedFloor],
      })
    }
    return listFor(input, [belowFloor])
  })
  vi.stubGlobal('fetch', fetchMock)
  renderPage(['records_officer'], 'PRR-2026-0042')

  const requestRow = await installmentRow()
  expect(
    within(requestRow).getByText(/Under the \$1\.00 waiver floor/i),
  ).toBeInTheDocument()
  // The prompt is a prompt: the fee is still assessed and still unpaid.
  expect(within(requestRow).getByRole('button', { name: 'Record payment' }))
    .toBeInTheDocument()
})

it('holds production behind an open deposit and records the deposit paid', async () => {
  const gates = [openProductionGate]
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url.startsWith('/api/staff/fee-schedules/')) {
      return response({
        count: 1, next: null, previous: null, results: [adoptedSchedule],
      })
    }
    if (url.startsWith('/api/staff/fee-deposit-gates/')) {
      if (options?.method === 'POST') {
        return response({ ...openProductionGate, is_open: false })
      }
      return response({
        count: gates.length, next: null, previous: null, results: gates,
      })
    }
    return listFor(input, [row])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  // Production is held: the officer is told the amount instead of offered a
  // Prepare button.
  const requestRow = await installmentRow()
  expect(
    within(requestRow).getByText(/\$36\.72 deposit required before production/i),
  ).toBeInTheDocument()
  expect(within(requestRow).queryByRole('button', { name: 'Prepare package' }))
    .not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Record deposit' }))
  const dialog = screen.getByRole('dialog', { name: 'Record the deposit' })
  await user.type(
    within(dialog).getByLabelText('Receipt reference'),
    'District receipt 2210',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Record deposit' }))

  const call = fetchMock.mock.calls.find(([input, options]) => (
    String(input) === `/api/staff/fee-deposit-gates/${openProductionGate.id}/record-payment/`
    && options?.method === 'POST'
  ))!
  expect(JSON.parse(String(call[1]?.body))).toEqual(expect.objectContaining({
    amount: '36.72',
    method: 'check',
    reference: 'District receipt 2210',
  }))
})

it('requires a substantive reason before lifting a deposit gate', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/fee-schedules/')) {
      return response({
        count: 1, next: null, previous: null, results: [adoptedSchedule],
      })
    }
    if (url.startsWith('/api/staff/fee-deposit-gates/')) {
      return response({
        count: 1, next: null, previous: null, results: [openProductionGate],
      })
    }
    return listFor(input, [row])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  await user.click(await screen.findByRole('button', { name: 'Lift' }))
  const dialog = screen.getByRole('dialog', { name: 'Lift the deposit gate' })
  const submit = within(dialog).getByRole('button', { name: 'Lift gate' })
  expect(submit).toBeDisabled()

  await user.type(within(dialog).getByLabelText('Reason'), 'short')
  expect(submit).toBeDisabled()

  await user.clear(within(dialog).getByLabelText('Reason'))
  await user.type(
    within(dialog).getByLabelText('Reason'),
    'Requester narrowed the request below the deposit threshold.',
  )
  expect(submit).toBeEnabled()
})

it('offers every release-ready record when the list spans more than one page', async () => {
  const records = Array.from({ length: 60 }, (_, index) => ({
    ...readyRecord,
    id: `aaaaaaaa-aaaa-4aaa-8aaa-${String(index).padStart(12, '0')}`,
    original_filename: `record-${index}.pdf`,
  }))
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/staff/record-items/')) {
      const second = url.includes('page=2')
      return response({
        count: records.length,
        next: second ? null : `${url}&page=2`,
        previous: null,
        results: second ? records.slice(50) : records.slice(0, 50),
      })
    }
    return listFor(input, [])
  })
  vi.stubGlobal('fetch', fetchMock)
  renderPage(['records_officer'], 'PRR-2026-0042')

  expect(await screen.findByLabelText('Select all 60')).toBeInTheDocument()
})

it('builds the release package in one action and previews it before release', async () => {
  let built = false
  const prepared = {
    ...row,
    item_count: 1,
    package_prepared_at: '2026-11-04T19:30:00Z',
    package_size: 4096,
    record_item_ids: [readyRecord.id],
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input) === '/api/staff/requests/PRR-2026-0042/release-package/' && options?.method === 'POST') {
      built = true
      return response(prepared)
    }
    return listFor(input, built ? [prepared] : [])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  expect(await screen.findByText(/1 record ready to release/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Build package' }))

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/staff/requests/PRR-2026-0042/release-package/',
    expect.objectContaining({ method: 'POST' }),
  )
  const preview = await screen.findByRole('list', { name: 'Records in this package' })
  expect(within(preview).getByText('board-minutes.pdf')).toBeInTheDocument()
  expect(screen.getByText(/No exemption log is needed/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Build package' })).not.toBeInTheDocument()
  const steps = screen.getByRole('list', { name: 'Release steps' })
  await user.click(within(steps).getByRole('button', { name: 'Release and send' }))
  expect(screen.getByRole('dialog', { name: 'Release installment' })).toBeInTheDocument()
})

it('shows a build blocker with a link to the tab that fixes it', async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input) === '/api/staff/requests/PRR-2026-0042/release-package/' && options?.method === 'POST') {
      return {
        ok: false,
        status: 400,
        text: async () => JSON.stringify({
          detail: 'No records are ready to release.',
          blocker: { code: 'no_ready_records', tab: 'review' },
        }),
      }
    }
    return listFor(input, [])
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()
  renderPage(['records_officer'], 'PRR-2026-0042')

  await user.click(await screen.findByRole('button', { name: 'Build package' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('No records are ready to release.')
  expect(screen.getByRole('link', { name: 'Go to Review to fix this' })).toHaveAttribute(
    'href', '/requests/PRR-2026-0042/review',
  )
})

it('folds the fee panels away when no fee applies, and leads with them when one does', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => listFor(input, [row])))
  const { unmount } = renderPage(['records_officer'], 'PRR-2026-0042')
  const summary = await screen.findByText('Fees and deposits — no fee applies')
  expect(summary.closest('details')).not.toHaveAttribute('open')
  unmount()

  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => listFor(input, [
    { ...row, fee_amount: '4.20', fee_balance: '4.20' },
  ])))
  renderPage(['records_officer'], 'PRR-2026-0042')
  expect(await screen.findByText('Adopted fee schedule')).toBeInTheDocument()
  expect(screen.queryByText('Fees and deposits — no fee applies')).not.toBeInTheDocument()
})
