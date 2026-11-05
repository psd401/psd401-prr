// ABOUTME: Regression tests for the preservation surface — state filters and hold copy.
// ABOUTME: A manual hold must never claim an email was sent without delivery evidence.
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeResponse, page, renderPage } from '../test/pageHarness'
import { Preservations } from './Preservations'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Preservations — state filters', () => {
  it('offers every preservation state and no nonexistent active state', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([]))))
    renderPage(<Preservations roles={['admin']} />)

    await screen.findByRole('button', { name: 'All' })
    for (const state of [
      'Pending', 'Held', 'Partial', 'Manual', 'No cameras', 'Expired', 'Released',
    ]) {
      expect(
        screen.getByRole('button', { name: new RegExp(`^${state}$`, 'i') }),
      ).toBeInTheDocument()
    }
    expect(screen.queryByRole('button', { name: /^active$/i })).not.toBeInTheDocument()
  })
})

describe('Preservations — hold copy', () => {
  it('does not claim that a manual-hold email was sent without delivery evidence', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
      {
        id: 1,
        request_number: 'PRR-2026-0001',
        state: 'manual',
        window_start: '2026-07-01T00:00:00-07:00',
        window_end: '2026-07-02T23:59:59-07:00',
        detected_dates_note: 'Camera API unavailable; human action required.',
        held_count: 0,
        camera_holds: [],
      },
    ]))))

    renderPage(<Preservations roles={['admin']} />)

    const preservationCard = (
      await screen.findByText('Footage preservation')
    ).closest('section')!
    expect(within(preservationCard).getByText(/manual/i)).toHaveTextContent(/manual hold required/i)
    expect(within(preservationCard).queryByText(/email sent/i)).not.toBeInTheDocument()
  })
})

const CONFIRMATION =
  '2 cameras at Example Ridge High School held from 2026-09-22 00:00 to 2026-09-23 00:00 '
  + 'Pacific time (Aware bookmarks, no expiry); 1 camera has a footage gap; 0 failed.'

function heldPreservation(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    request_number: 'PRR-2026-0007',
    state: 'partial',
    window_start: '2026-09-22T07:00:00Z',
    window_end: '2026-09-23T07:00:00Z',
    detected_dates_note: '',
    site_codes: ['XRH'],
    site_resolution:
      'Sites named in the request: Example Ridge High School (XRH). Holding every camera in those Aware groups.',
    held_count: 2,
    confirmation_text: CONFIRMATION,
    request_closed: true,
    released_at: null,
    released_by: null,
    release_reason: '',
    camera_holds: [
      {
        id: 1, camera_id: 'cam-1', camera_name: 'Gym Entrance', site: 'XRH',
        clip_id: 'bm-1', state: 'held', error: '', coverage_fraction: 1,
        coverage_note: '', excluded_at: null, excluded_by: null, exclusion_reason: '',
      },
      {
        id: 2, camera_id: 'cam-2', camera_name: 'Staff Parking', site: 'XRH',
        clip_id: 'bm-2', state: 'gap', error: '', coverage_fraction: 0.4,
        coverage_note: 'Short coverage: head 0: 40%', excluded_at: null,
        excluded_by: null, exclusion_reason: '',
      },
    ],
    ...overrides,
  }
}

describe('Preservations — Aware bookmark holds', () => {
  it('shows the site scope, coverage, and bookmark for each camera', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([heldPreservation()]))))
    renderPage(<Preservations roles={['records_officer']} />)

    expect(await screen.findByText(/Holding every camera in those Aware groups/))
      .toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Aware bookmark' })).toBeInTheDocument()
    expect(screen.getByText('bm-2')).toBeInTheDocument()
    expect(screen.getByText('Footage gap')).toBeInTheDocument()
    expect(screen.getByText('40%')).toBeInTheDocument()
    expect(screen.getByText('Short coverage: head 0: 40%')).toBeInTheDocument()
  })

  it('offers the confirmation text to copy and never sends it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([heldPreservation()]))))
    const user = userEvent.setup()
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    renderPage(<Preservations roles={['records_officer']} />)

    expect(await screen.findByText(CONFIRMATION)).toBeInTheDocument()
    expect(screen.getByText(/never sent automatically/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Copy confirmation' }))
    expect(writeText).toHaveBeenCalledWith(CONFIRMATION)
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })

  it('shows no confirmation when nothing is confirmed held', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
      heldPreservation({ state: 'no_cameras', confirmation_text: null, camera_holds: [] }),
    ]))))
    renderPage(<Preservations roles={['records_officer']} />)

    expect(await screen.findByText('No cameras in Aware at the requested site'))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy confirmation' })).not.toBeInTheDocument()
  })
})

describe('Preservations — release', () => {
  it('offers release only to a Records Officer on a closed request', async () => {
    for (const [roles, overrides] of [
      [['admin'], {}],
      [['records_officer'], { request_closed: false }],
      [['records_officer'], { state: 'manual' }],
    ] as const) {
      vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
        heldPreservation(overrides),
      ]))))
      const view = renderPage(<Preservations roles={[...roles]} />)
      await screen.findByText('Footage preservation')
      expect(screen.queryByRole('button', { name: 'Release hold' })).not.toBeInTheDocument()
      view.unmount()
    }
  })

  it('requires a reason and the litigation-hold attestation before releasing', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/release/') && init?.method === 'POST') {
        return fakeResponse(heldPreservation({ state: 'released', confirmation_text: null }))
      }
      return fakeResponse(page([heldPreservation()]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage(<Preservations roles={['records_officer']} />)

    await user.click(await screen.findByRole('button', { name: 'Release hold' }))
    const dialog = await screen.findByRole('dialog')
    const submit = within(dialog).getByRole('button', { name: 'Release and delete bookmarks' })
    expect(submit).toBeDisabled()

    await user.type(
      within(dialog).getByLabelText(/Reason/),
      'Request fulfilled; the requester confirmed receipt.',
    )
    expect(submit).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox', { name: /litigation hold/i }))
    expect(submit).toBeEnabled()
    await user.click(submit)

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/release/'))
      expect(call).toBeDefined()
      expect(String(call![0])).toBe('/api/staff/preservations/7/release/')
      expect(call![1]?.method).toBe('POST')
      expect(JSON.parse(String(call![1]?.body))).toEqual({
        reason: 'Request fulfilled; the requester confirmed receipt.',
        attest_no_litigation_hold: true,
      })
    })
  })
})
