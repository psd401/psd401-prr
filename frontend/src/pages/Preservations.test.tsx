// ABOUTME: Regression tests for the preservation surface — state filters and hold copy.
// ABOUTME: A manual hold must never claim an email was sent without delivery evidence.
import { screen, within } from '@testing-library/react'
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
    for (const state of ['Pending', 'Held', 'Partial', 'Manual', 'Expired', 'Released']) {
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
