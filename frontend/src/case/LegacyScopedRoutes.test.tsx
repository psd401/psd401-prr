// ABOUTME: Compatibility-route contracts for the /correspondence entry point.
// ABOUTME: Redirects replace history and retain only safe canonical case context.
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router'
import { describe, expect, it } from 'vitest'
import { LegacyCorrespondenceRedirect } from './LegacyScopedRoutes'

function LocationProbe() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output aria-label="Location">{location.pathname}{location.search}</output>
      <button type="button" onClick={() => navigate(-1)}>Back</button>
    </>
  )
}

describe('LegacyCorrespondenceRedirect', () => {
  function renderCorrespondence(route: string, roles: Parameters<typeof LegacyCorrespondenceRedirect>[0]['roles']) {
    return render(
      <MemoryRouter initialEntries={['/cases', route]} initialIndex={1}>
        <Routes>
          <Route
            path="/correspondence"
            element={<LegacyCorrespondenceRedirect roles={roles} />}
          />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('moves request-scoped correspondence to Messages with safe filters', async () => {
    renderCorrespondence(
      '/correspondence?request=PRR-2026-0042&message=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa&status=failed&unsafe=drop-me',
      ['liaison'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/requests/PRR-2026-0042/messages?message=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa&message_status=failed',
    )
  })

  it('maps the legacy delivery-status pseudo-state to the canonical message kind', async () => {
    renderCorrespondence(
      '/correspondence?request=PRR-2026-0042&status=delivery_status_notification',
      ['records_officer'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/requests/PRR-2026-0042/messages?message_kind=delivery_status_notification',
    )
  })

  it('preserves valid false attachment, delivery, date, and assignee filters', async () => {
    renderCorrespondence(
      '/correspondence?request=PRR-2026-0042&has_attachments=false&delivery_state=retryable&date_from=2026-02-28&assignee=42',
      ['records_officer'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/requests/PRR-2026-0042/messages?message_delivery_state=retryable&message_has_attachments=false&message_date_from=2026-02-28&message_assignee=42',
    )
  })

  it('drops filters that the correspondence API would reject', async () => {
    renderCorrespondence(
      '/correspondence?request=PRR-2026-0042&delivery_state=bogus&date_from=0000-01-01&date_to=2026-99-99&assignee=alice',
      ['records_officer'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      /^\/requests\/PRR-2026-0042\/messages$/,
    )
  })

  it('drops a reversed date range that the correspondence API would reject', async () => {
    renderCorrespondence(
      '/correspondence?request=PRR-2026-0042&date_from=2026-03-02&date_to=2026-03-01',
      ['records_officer'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      /^\/requests\/PRR-2026-0042\/messages$/,
    )
  })

  it('moves inbound receipt review to the Inbox', async () => {
    renderCorrespondence(
      '/correspondence?inbound_receipt=99999999-9999-4999-8999-999999999999',
      ['records_officer'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/inbox?view=unmatched&inbound_receipt=99999999-9999-4999-8999-999999999999',
    )
  })

  it('gives an explicit inbound receipt precedence over request message scope', async () => {
    renderCorrespondence(
      '/correspondence?request=PRR-2026-0042&inbound_receipt=99999999-9999-4999-8999-999999999999',
      ['records_officer'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/inbox?view=unmatched&inbound_receipt=99999999-9999-4999-8999-999999999999',
    )
  })

  it.each([
    ['status=quarantined', 'quarantined'],
    ['matching_state=quarantined', 'quarantined'],
    ['status=unmatched', 'unmatched'],
    ['matching_state=unmatched', 'unmatched'],
    ['matching_state=candidate', 'unmatched'],
    ['matching_state=ambiguous', 'unmatched'],
  ])(
    'moves legacy inbound filter %s to the %s Inbox view',
    async (query, view) => {
      renderCorrespondence(`/correspondence?${query}`, ['records_officer'])

      expect(await screen.findByLabelText('Location')).toHaveTextContent(
        `/inbox?view=${view}`,
      )
    },
  )

  it('keeps a valid receipt selected in its quarantined Inbox view', async () => {
    renderCorrespondence(
      '/correspondence?status=quarantined&inbound_receipt=99999999-9999-4999-8999-999999999999',
      ['records_officer'],
    )

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/inbox?view=quarantined&inbound_receipt=99999999-9999-4999-8999-999999999999',
    )
  })

  it('moves transport operations to Configuration for administrators', async () => {
    renderCorrespondence('/correspondence', ['admin'])

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/configuration/correspondence',
    )
  })

  it('moves failed transport work to the focused operations queue', async () => {
    renderCorrespondence('/correspondence?status=failed', ['records_officer'])

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/configuration/correspondence?focus=failed',
    )
  })

  it('preserves failed delivery bookmarks for administrators', async () => {
    renderCorrespondence('/correspondence?status=failed', ['admin'])

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/configuration/correspondence?focus=failed',
    )
  })

  it('does not mislabel queued transport state as a delivery exception', async () => {
    renderCorrespondence('/correspondence?status=queued', ['records_officer'])

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/configuration/correspondence?focus=queued',
    )
  })

  it('preserves the legacy draft transport queue', async () => {
    renderCorrespondence('/correspondence?status=draft', ['records_officer'])

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/configuration/correspondence?focus=draft',
    )
  })

  it('preserves the legacy received-message queue in Inbox', async () => {
    renderCorrespondence('/correspondence?status=received', ['records_officer'])

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/inbox?view=received',
    )
  })

  it('preserves queued transport bookmarks for administrators', async () => {
    renderCorrespondence('/correspondence?status=queued', ['admin'])

    expect(await screen.findByLabelText('Location')).toHaveTextContent(
      '/configuration/correspondence?focus=queued',
    )
  })
})
