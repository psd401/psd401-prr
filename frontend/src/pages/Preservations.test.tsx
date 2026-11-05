// ABOUTME: Regression tests for the preservation surface — state filters and hold copy.
// ABOUTME: A manual hold must never claim an email was sent without delivery evidence.
import type { ReactNode } from 'react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { fakeResponse, page, queryClient, renderPage } from '../test/pageHarness'
import { Preservations } from './Preservations'

/** renderPage, but with a QueryClient the test can spy on. */
function renderWithClient(ui: ReactNode, client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

function spyOnInvalidate(client: QueryClient) {
  return vi.spyOn(client, 'invalidateQueries')
}

function invalidatedKeys(spy: ReturnType<typeof spyOnInvalidate>) {
  return spy.mock.calls.map(([filters]) => filters?.queryKey)
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Preservations — state filters', () => {
  it('offers every preservation state and no nonexistent active state', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([]))))
    renderPage(<Preservations roles={['admin']} />)

    const filters = await screen.findByRole('group', { name: 'Preservation state filters' })
    within(filters).getByRole('button', { name: 'All' })
    // Filter names come from the same labels as the state badges.
    for (const label of [
      'Pending',
      'Held',
      'Partly held',
      'Manual hold required',
      'No cameras in Aware at the requested site',
      'Window outside retention',
      'Released',
    ]) {
      expect(within(filters).getByRole('button', { name: label })).toBeInTheDocument()
    }
    expect(within(filters).queryByRole('button', { name: /^active$/i })).not.toBeInTheDocument()
    expect(within(filters).queryByRole('button', { name: /_/ })).not.toBeInTheDocument()
  })
})

describe('Preservations — exclusion', () => {
  it('offers no Exclude once the hold is released or expired', async () => {
    const base = heldPreservation()
    for (const [state, cameraState] of [
      ['released', 'failed'],
      ['released', 'release_failed'],
      ['expired', 'not_retained'],
    ] as const) {
      vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
        heldPreservation({
          state,
          releasable: false,
          confirmation_text: null,
          camera_holds: [{ ...base.camera_holds[1], state: cameraState }],
        }),
      ]))))
      const view = renderPage(<Preservations roles={['records_officer']} />)
      await screen.findByText('Staff Parking')
      expect(screen.queryByRole('button', { name: 'Exclude' })).not.toBeInTheDocument()
      view.unmount()
    }
  })
})

describe('Preservations — states that need a human', () => {
  it('says a human must act, in a warning tone, for every unresolved state', async () => {
    const base = heldPreservation()
    const camera = (id: number, state: string) => ({
      ...base.camera_holds[0], id, camera_name: `Camera ${id}`, state,
    })
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
      heldPreservation({ id: 1, state: 'no_cameras', camera_holds: [] }),
      heldPreservation({ id: 2, state: 'expired', camera_holds: [] }),
      heldPreservation({
        id: 3,
        state: 'partial',
        camera_holds: [camera(31, 'gap'), camera(32, 'not_retained'), camera(33, 'release_failed')],
      }),
    ]))))
    renderPage(<Preservations roles={['admin']} />)

    for (const label of [
      'No cameras in Aware at the requested site — adequate-search review required',
      'Window outside retention — adequate-search review required',
      'Partly held — gaps or failures need review',
      'Footage gap — adequate-search review required',
      'No retained footage — adequate-search review required',
      'Release failed — bookmark may still exist; retry the release',
    ]) {
      const badge = await screen.findByText(label)
      expect(badge).toHaveClass('nx-badge')
      expect(badge).not.toHaveClass('nx-badge--plain')
    }
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
    releasable: true,
    widenable: false,
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
    expect(screen.getByText('Footage gap — adequate-search review required')).toBeInTheDocument()
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
  it('offers release only to a Records Officer when the server says it is releasable', async () => {
    for (const [roles, overrides] of [
      [['admin'], {}],
      // A closed request can still be unreleasable (merged-target rule).
      [['records_officer'], { request_closed: true, releasable: false }],
      [['records_officer'], { state: 'manual', releasable: false }],
    ] as const) {
      vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
        heldPreservation(overrides),
      ]))))
      const view = renderPage(<Preservations roles={[...roles]} />)
      await screen.findByText('Footage preservation')
      expect(screen.queryByRole('button', { name: /Release hold|Retry release/ }))
        .not.toBeInTheDocument()
      view.unmount()
    }
  })

  it('trusts the server releasable flag over request_closed', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
      heldPreservation({ request_closed: false, releasable: true }),
    ]))))
    renderPage(<Preservations roles={['records_officer']} />)
    expect(await screen.findByRole('button', { name: /Release hold/ })).toBeInTheDocument()
  })

  it('offers "Retry release" when a released hold still has release_failed cameras', async () => {
    const base = heldPreservation()
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
      heldPreservation({
        state: 'released',
        confirmation_text: null,
        releasable: true,
        released_at: '2026-10-01T17:00:00Z',
        released_by: 'records.officer',
        release_reason: 'Request fulfilled and closed.',
        camera_holds: [
          { ...base.camera_holds[0], state: 'released' },
          { ...base.camera_holds[1], state: 'release_failed', error: 'Bookmark delete timed out.' },
        ],
      }),
    ]))))
    renderPage(<Preservations roles={['records_officer']} />)

    expect(await screen.findByRole('button', { name: /Retry release/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Release hold/ })).not.toBeInTheDocument()
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

describe('Preservations — release dialog', () => {
  async function openReleaseDialog() {
    const user = userEvent.setup()
    renderPage(<Preservations roles={['records_officer']} />)
    await user.click(await screen.findByRole('button', { name: /Release hold/ }))
    const dialog = await screen.findByRole('dialog')
    return { user, dialog }
  }

  it('shows a server refusal inside the dialog', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/release/') && init?.method === 'POST') {
        return fakeResponse({ detail: 'This request was merged into another; release that one.' }, 400)
      }
      return fakeResponse(page([heldPreservation()]))
    }))
    const { user, dialog } = await openReleaseDialog()
    await user.type(within(dialog).getByLabelText(/Reason/), 'Request fulfilled and closed.')
    await user.click(within(dialog).getByRole('checkbox', { name: /litigation hold/i }))
    await user.click(within(dialog).getByRole('button', { name: 'Release and delete bookmarks' }))

    expect(await within(dialog).findByRole('alert'))
      .toHaveTextContent('This request was merged into another; release that one.')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('caps the reason at the server limit of 500 characters', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([heldPreservation()]))))
    const { dialog } = await openReleaseDialog()
    expect(within(dialog).getByLabelText(/Reason/)).toHaveAttribute('maxLength', '500')
  })

  it('cannot be cancelled while the release is in flight', async () => {
    let finish: (value: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/release/') && init?.method === 'POST') {
        return new Promise<Response>((resolve) => { finish = resolve })
      }
      return Promise.resolve(fakeResponse(page([heldPreservation()])))
    }))
    const { user, dialog } = await openReleaseDialog()
    await user.type(within(dialog).getByLabelText(/Reason/), 'Request fulfilled and closed.')
    await user.click(within(dialog).getByRole('checkbox', { name: /litigation hold/i }))
    await user.click(within(dialog).getByRole('button', { name: 'Release and delete bookmarks' }))

    expect(await within(dialog).findByRole('button', { name: 'Releasing…' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    finish(fakeResponse({ detail: 'Release refused.' }, 400))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Release refused.')
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })
})

describe('Preservations — widen to district-wide', () => {
  function narrowed(overrides: Record<string, unknown> = {}) {
    return heldPreservation({ request_closed: false, releasable: false, widenable: true, ...overrides })
  }

  it('offers widening only to a Records Officer on a widenable hold', async () => {
    for (const [roles, overrides] of [
      [['admin'], {}],
      [['records_officer'], { widenable: false }],
    ] as const) {
      vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([narrowed(overrides)]))))
      const view = renderPage(<Preservations roles={[...roles]} />)
      await screen.findByText('Footage preservation')
      expect(screen.queryByRole('button', { name: /Widen/ })).not.toBeInTheDocument()
      view.unmount()
    }
  })

  it('widens with a substantive reason and refreshes the hold and the request', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/widen/') && init?.method === 'POST') {
        return fakeResponse(narrowed({ widenable: false, site_codes: [] }))
      }
      return fakeResponse(page([narrowed()]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const client = queryClient()
    const invalidate = spyOnInvalidate(client)
    const user = userEvent.setup()
    renderWithClient(<Preservations roles={['records_officer']} />, client)

    await user.click(await screen.findByRole('button', { name: /Widen to district-wide.*PRR-2026-0007/ }))
    const dialog = await screen.findByRole('dialog')
    const reason = within(dialog).getByLabelText(/Reason/)
    expect(reason).toHaveAttribute('maxLength', '500')
    const submit = within(dialog).getByRole('button', { name: 'Widen hold' })
    await user.type(reason, 'too short')
    expect(submit).toBeDisabled()
    await user.type(reason, ' — the requester named the wrong school.')
    expect(submit).toBeEnabled()
    await user.click(submit)

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/widen/'))
      expect(call).toBeDefined()
      expect(String(call![0])).toBe('/api/staff/preservations/7/widen/')
      expect(call![1]?.method).toBe('POST')
      expect(JSON.parse(String(call![1]?.body))).toEqual({
        reason: 'too short — the requester named the wrong school.',
      })
    })
    await waitFor(() => {
      const keys = invalidatedKeys(invalidate)
      expect(keys).toContainEqual(['preservations'])
      expect(keys).toContainEqual(['request', 'PRR-2026-0007'])
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps the dialog open while pending and shows a server refusal', async () => {
    let finish: (value: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/widen/') && init?.method === 'POST') {
        return new Promise<Response>((resolve) => { finish = resolve })
      }
      return Promise.resolve(fakeResponse(page([narrowed()])))
    }))
    const user = userEvent.setup()
    renderPage(<Preservations roles={['records_officer']} />)

    await user.click(await screen.findByRole('button', { name: /Widen to district-wide/ }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Reason/), 'The incident location is unknown.')
    await user.click(within(dialog).getByRole('button', { name: 'Widen hold' }))

    expect(await within(dialog).findByRole('button', { name: 'Widening…' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    finish(fakeResponse({ detail: 'This hold is already district-wide.' }, 400))
    expect(await within(dialog).findByRole('alert'))
      .toHaveTextContent('This hold is already district-wide.')
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })
})

describe('Preservations — cache refresh after a change', () => {
  it('refreshes the request detail after a release so it stops saying "Held"', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/release/') && init?.method === 'POST') {
        return fakeResponse(heldPreservation({ state: 'released', confirmation_text: null }))
      }
      return fakeResponse(page([heldPreservation()]))
    }))
    const client = queryClient()
    const invalidate = spyOnInvalidate(client)
    const user = userEvent.setup()
    renderWithClient(<Preservations roles={['records_officer']} />, client)

    await user.click(await screen.findByRole('button', { name: /Release hold/ }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Request fulfilled and closed.')
    await user.click(within(dialog).getByRole('checkbox', { name: /litigation hold/i }))
    await user.click(within(dialog).getByRole('button', { name: 'Release and delete bookmarks' }))

    await waitFor(() => {
      const keys = invalidatedKeys(invalidate)
      expect(keys).toContainEqual(['preservations'])
      expect(keys).toContainEqual(['request', 'PRR-2026-0007'])
    })
  })

  it('refreshes the request detail after a camera exclusion', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/exclude-camera/') && init?.method === 'POST') {
        return fakeResponse(heldPreservation().camera_holds[1])
      }
      return fakeResponse(page([heldPreservation({ request_closed: false, releasable: false })]))
    }))
    const client = queryClient()
    const invalidate = spyOnInvalidate(client)
    const user = userEvent.setup()
    renderWithClient(<Preservations roles={['records_officer']} />, client)

    await screen.findByText('Staff Parking')
    await user.click(screen.getAllByRole('button', { name: 'Exclude' })[1]!)
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Camera faces a private office.')
    await user.click(within(dialog).getByRole('button', { name: 'Exclude camera' }))

    await waitFor(() => {
      const keys = invalidatedKeys(invalidate)
      expect(keys).toContainEqual(['preservations'])
      expect(keys).toContainEqual(['request', 'PRR-2026-0007'])
    })
  })
})
