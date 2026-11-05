// ABOUTME: Footage preservation holds screen — the retention race dashboard.
// ABOUTME: Cards per Aware bookmark hold: cameras, exclusions, confirmation, release.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import type { StaffRole } from '../api/auth'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  MIN_REASON_LENGTH,
  Modal,
  Spinner,
  Table,
  Textarea,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
  useToast,
} from '../ui'

interface CameraHold {
  id: number
  camera_id: string
  camera_name: string
  site: string
  clip_id: string | null
  state: string
  error: string
  coverage_fraction: number | null
  coverage_note: string
  excluded_at: string | null
  excluded_by: string | null
  exclusion_reason: string
}

interface Preservation {
  id: number
  request_number: string
  state: string
  window_start: string
  window_end: string
  detected_dates_note: string
  site_codes: string[]
  site_resolution: string
  held_count: number
  camera_holds: CameraHold[]
  confirmation_text: string | null
  request_closed: boolean
  /** Server-computed: closed, not a merged target, and held/partial — or
   * released with cameras whose bookmark delete failed (a retry). */
  releasable: boolean
  /** Server-computed: narrowed to specific sites and not released. */
  widenable: boolean
  released_at: string | null
  released_by: string | null
  release_reason: string
}

const PRESERVATION_STATES = [
  'pending',
  'held',
  'partial',
  'manual',
  'no_cameras',
  'expired',
  'released',
] as const

/** The release and widen endpoints reject reasons longer than this. */
const MAX_REASON_LENGTH = 500

/** A release already ran but some Aware bookmarks may still exist. */
function hasReleaseFailures(preservation: Preservation) {
  return preservation.camera_holds.some((h) => h.state === 'release_failed')
}

const STATE_TONES: Record<string, 'green' | 'amber' | 'red' | 'neutral'> = {
  pending: 'amber',
  held: 'green',
  partial: 'amber',
  manual: 'amber',
  no_cameras: 'amber',
  expired: 'red',
  released: 'neutral',
  excluded: 'neutral',
  error: 'red',
  failed: 'red',
  gap: 'amber',
  not_retained: 'amber',
  release_failed: 'red',
}

// Every state that leaves footage unaccounted for says a human must act:
// nothing here is resolved by waiting.
const ADEQUATE_SEARCH = 'adequate-search review required'

const STATE_LABELS: Record<string, string> = {
  pending: 'Pending — hold executing',
  held: 'Held',
  partial: 'Partly held — gaps or failures need review',
  manual: 'Manual hold required',
  no_cameras: `No cameras in Aware at the requested site — ${ADEQUATE_SEARCH}`,
  expired: `Window outside retention — ${ADEQUATE_SEARCH}`,
  released: 'Released',
  // Camera-level states
  gap: `Footage gap — ${ADEQUATE_SEARCH}`,
  not_retained: `No retained footage — ${ADEQUATE_SEARCH}`,
  failed: 'Failed',
  release_failed: 'Release failed — bookmark may still exist; retry the release',
}

/** Filter buttons use the badge label up to its "— detail" suffix. */
function filterLabel(state: string) {
  return (STATE_LABELS[state] ?? state).split(' — ')[0]
}

function StateBadge({ state }: { state: string }) {
  return <Badge tone={STATE_TONES[state] ?? 'neutral'}>{STATE_LABELS[state] ?? state}</Badge>
}

interface ExcludeTarget {
  preservationId: number
  requestNumber: string
  hold: CameraHold
}

function coverageLabel(fraction: number | null) {
  return fraction === null ? '—' : `${Math.round(fraction * 100)}%`
}

/** Text a Records Officer may paste into a letter. Copy only: never sent. */
function ConfirmationText({ text }: { text: string }) {
  const toast = useToast()
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      if (!navigator.clipboard) throw new Error('Clipboard API unavailable')
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      toast('error', 'Copy failed — select the text and copy it by hand.')
    }
  }

  return (
    <div className="mb-4 rounded-md border border-line bg-surface-sunken px-3 py-2">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="text-sm text-ink">{text}</p>
        <Button
          type="button"
          variant="ghost"
          onClick={copy}
          aria-label={copied ? 'Copied' : 'Copy confirmation'}
        >
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <p className="mt-1 text-xs text-ink-muted">
        For correspondence you write. This text is never sent automatically.
      </p>
    </div>
  )
}

/** Records Officer controls in a preservation card's header. */
function HoldActions({
  preservation,
  onRelease,
  onWiden,
}: {
  preservation: Preservation
  onRelease: () => void
  onWiden: () => void
}) {
  return (
    <>
      {preservation.widenable && (
        <Button
          variant="ghost"
          onClick={onWiden}
          aria-label={`Widen to district-wide — ${preservation.request_number}`}
        >
          Widen to district-wide
        </Button>
      )}
      {preservation.releasable && (
        <Button variant="ghost" onClick={onRelease}>
          {hasReleaseFailures(preservation) ? 'Retry release' : 'Release hold'}
        </Button>
      )}
    </>
  )
}

/**
 * Widen a site-narrowed hold to every camera in the district. Used when the
 * site the requester named turns out to be wrong or unknown.
 */
function WidenDialog({
  preservation,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  preservation: Preservation
  pending: boolean
  error: unknown
  onClose: () => void
  onSubmit: (reason: string) => void
}) {
  const [reason, setReason] = useState('')

  function submit(e: FormEvent) {
    e.preventDefault()
    if (pending || reasonTooShort(reason)) return
    onSubmit(reason)
  }

  return (
    <Modal
      open
      title={`Widen preservation for ${preservation.request_number}`}
      onClose={onClose}
      dismissible={!pending}
    >
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm text-ink-muted">
          This hold covers only the sites named in the request. Widening it
          bookmarks every camera in the district for the same window.
        </p>
        <Field
          label="Reason"
          hint={reasonHint('say why the named sites may not cover the footage')}
        >
          <Textarea
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            minLength={MIN_REASON_LENGTH}
            maxLength={MAX_REASON_LENGTH}
            required
          />
        </Field>
        {reasonShortfall(reason) && (
          <p aria-live="polite" className="text-sm text-ink-muted">{reasonShortfall(reason)}</p>
        )}
        {error != null && <ErrorNote error={error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button type="submit" disabled={pending || reasonTooShort(reason)}>
            {pending ? 'Widening…' : 'Widen hold'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export function Preservations({
  roles,
  requestNumber,
}: {
  roles: StaffRole[]
  requestNumber?: string
}) {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const canExclude = roles.includes('records_officer')
  const toast = useToast()
  const activeStateFilter = searchParams.get('preservation_state') ?? ''
  const [excluding, setExcluding] = useState<ExcludeTarget | null>(null)
  const [reason, setReason] = useState('')
  const [releasing, setReleasing] = useState<Preservation | null>(null)
  const [releaseReason, setReleaseReason] = useState('')
  const [attested, setAttested] = useState(false)
  const [widening, setWidening] = useState<Preservation | null>(null)

  const list = useQuery({
    queryKey: ['preservations', requestNumber ?? 'all', activeStateFilter],
    queryFn: () => {
      const params = new URLSearchParams()
      if (requestNumber) params.set('request', requestNumber)
      if (activeStateFilter) params.set('state', activeStateFilter)
      const query = params.toString()
      return (
      api<Page<Preservation> | Preservation[]>(
          `/api/staff/preservations/${query ? `?${query}` : ''}`,
        )
      )
    },
  })

  // The request detail card shows the hold state and confirmation text too;
  // leaving it cached would keep saying "Held" after a release.
  function refreshAfterChange(requestNumber: string) {
    qc.invalidateQueries({ queryKey: ['preservations'] })
    qc.invalidateQueries({ queryKey: ['request', requestNumber] })
  }

  const exclude = useMutation({
    mutationFn: ({ preservationId, holdId, reason }: { preservationId: number; requestNumber: string; holdId: number; reason: string }) =>
      api<CameraHold>(`/api/staff/preservations/${preservationId}/exclude-camera/`, {
        method: 'POST',
        body: { camera_hold_id: holdId, reason },
      }),
    onSuccess: (_hold, { requestNumber }) => {
      refreshAfterChange(requestNumber)
      toast('success', 'Camera excluded from preservation hold.')
      closeExclude()
    },
  })

  const release = useMutation({
    mutationFn: ({ preservationId, reason }: { preservationId: number; requestNumber: string; reason: string }) =>
      api<Preservation>(`/api/staff/preservations/${preservationId}/release/`, {
        method: 'POST',
        body: { reason, attest_no_litigation_hold: true },
      }),
    onSuccess: (_preservation, { requestNumber }) => {
      refreshAfterChange(requestNumber)
      toast('success', 'Preservation hold released; Aware bookmarks are being deleted.')
      closeRelease()
    },
  })

  const widen = useMutation({
    mutationFn: ({ preservationId, reason }: { preservationId: number; requestNumber: string; reason: string }) =>
      api<Preservation>(`/api/staff/preservations/${preservationId}/widen/`, {
        method: 'POST',
        body: { reason },
      }),
    onSuccess: (_preservation, { requestNumber }) => {
      refreshAfterChange(requestNumber)
      toast('success', 'Preservation hold widened to district-wide.')
      closeWiden()
    },
  })

  function openWiden(preservation: Preservation) {
    widen.reset()
    setWidening(preservation)
  }

  function closeWiden() {
    setWidening(null)
    widen.reset()
  }

  function openRelease(preservation: Preservation) {
    setReleasing(preservation)
    setReleaseReason('')
    setAttested(false)
    release.reset()
  }

  function closeRelease() {
    setReleasing(null)
    setReleaseReason('')
    setAttested(false)
    release.reset()
  }

  function submitRelease(e: FormEvent) {
    e.preventDefault()
    if (!releasing || reasonTooShort(releaseReason) || !attested) return
    release.mutate({
      preservationId: releasing.id,
      requestNumber: releasing.request_number,
      reason: releaseReason,
    })
  }

  function openExclude(preservationId: number, requestNumber: string, hold: CameraHold) {
    setExcluding({ preservationId, requestNumber, hold })
    setReason('')
    exclude.reset()
  }

  function closeExclude() {
    setExcluding(null)
    setReason('')
    exclude.reset()
  }

  function submitExclude(e: FormEvent) {
    e.preventDefault()
    if (!excluding || !reason.trim()) return
    exclude.mutate({
      preservationId: excluding.preservationId,
      requestNumber: excluding.requestNumber,
      holdId: excluding.hold.id,
      reason,
    })
  }

  function changeStateFilter(value: string) {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      if (value) next.set('preservation_state', value)
      else next.delete('preservation_state')
      return next
    })
  }

  if (list.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />

  const rows = unwrap(list.data)

  return (
    <>

      <div
        role="group"
        aria-label="Preservation state filters"
        className="mb-6 flex flex-wrap gap-2"
      >
        <Button
          variant={activeStateFilter === '' ? 'primary' : 'ghost'}
          onClick={() => changeStateFilter('')}
          aria-pressed={activeStateFilter === ''}
        >
          All
        </Button>
        {PRESERVATION_STATES.map((s) => (
          <Button
            key={s}
            variant={activeStateFilter === s ? 'primary' : 'ghost'}
            onClick={() => changeStateFilter(s)}
            aria-pressed={activeStateFilter === s}
          >
            {filterLabel(s)}
          </Button>
        ))}
      </div>

      {rows.length === 0 ? (
        <EmptyState>No preservations match this filter.</EmptyState>
      ) : (
        <div className="space-y-6">
          {rows.map((p) => (
            <Card key={p.id}>
              <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="font-heading text-lg font-bold text-ink">
                    Footage preservation
                  </h3>
                  <p className="mt-1 text-sm text-ink-muted">
                    {p.window_start} → {p.window_end}
                  </p>
                  {p.site_resolution && (
                    <p className="mt-1 text-sm text-ink-muted">{p.site_resolution}</p>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <StateBadge state={p.state} />
                  {canExclude && (
                    <HoldActions
                      preservation={p}
                      onRelease={() => openRelease(p)}
                      onWiden={() => openWiden(p)}
                    />
                  )}
                </div>
              </div>

              {p.confirmation_text && <ConfirmationText text={p.confirmation_text} />}

              {p.released_at && (
                <p className="mb-4 text-sm text-ink-muted">
                  Released {p.released_at.slice(0, 10)} by {p.released_by ?? '—'}
                  {p.release_reason && <> — {p.release_reason}</>}
                </p>
              )}

              {p.detected_dates_note && (
                <p className="mb-4 rounded-md border border-line bg-surface-sunken px-3 py-2 text-sm text-ink-muted">
                  {p.detected_dates_note}
                </p>
              )}

              <p className="mb-2 font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                Camera holds ({p.held_count})
              </p>

              {p.camera_holds.length === 0 ? (
                <EmptyState>No camera holds recorded.</EmptyState>
              ) : (
                <Table
                  columns={[
                    {
                      header: 'Camera',
                      cell: (h: CameraHold) => <span className="font-heading font-bold">{h.camera_name}</span>,
                    },
                    { header: 'Site', cell: (h: CameraHold) => h.site },
                    { header: 'State', cell: (h: CameraHold) => <StateBadge state={h.state} /> },
                    {
                      header: 'Aware bookmark',
                      cell: (h: CameraHold) =>
                        h.clip_id ? h.clip_id : <span className="text-ink-muted">—</span>,
                    },
                    {
                      header: 'Coverage',
                      cell: (h: CameraHold) => (
                        <span className="block max-w-xs text-xs">
                          <span>{coverageLabel(h.coverage_fraction)}</span>
                          {h.coverage_note && (
                            <span className="block text-ink-muted">{h.coverage_note}</span>
                          )}
                        </span>
                      ),
                    },
                    {
                      header: 'Error',
                      cell: (h: CameraHold) =>
                        h.error ? (
                          <span className="text-danger-ink">{h.error}</span>
                        ) : (
                          <span className="text-ink-muted">—</span>
                        ),
                    },
                    {
                      header: 'Excluded',
                      cell: (h: CameraHold) =>
                        h.excluded_at ? (
                          <span className="block max-w-xs text-xs text-ink-muted">
                            {h.excluded_at.slice(0, 10)} by {h.excluded_by ?? '—'}
                            {h.exclusion_reason && <> — {h.exclusion_reason}</>}
                          </span>
                        ) : (
                          <span className="text-ink-muted">—</span>
                        ),
                    },
                    {
                      header: '',
                      align: 'right',
                      cell: (h: CameraHold) =>
                        h.excluded_at || h.state === 'released' || !canExclude ? null : (
                          <Button variant="ghost" onClick={() => openExclude(p.id, p.request_number, h)}>
                            Exclude
                          </Button>
                        ),
                    },
                  ]}
                  rows={p.camera_holds}
                  rowKey={(h) => h.id}
                />
              )}

              <p className="mt-3 text-xs text-ink-muted">
                Exclusions and releases are logged with your name and reason — RCW 42.56 audit trail.
              </p>
            </Card>
          ))}
        </div>
      )}

      <Modal
        open={canExclude && excluding !== null}
        title={excluding ? `Exclude ${excluding.hold.camera_name}` : ''}
        onClose={closeExclude}
      >
        {excluding && (
          <form onSubmit={submitExclude} className="space-y-4">
            <p className="text-sm text-ink-muted">
              Camera <span className="font-heading font-bold">{excluding.hold.camera_name}</span> at{' '}
              {excluding.hold.site} will be excluded from this preservation hold.
            </p>
            <Field label="Reason" hint="Required — recorded in the audit trail.">
              <Textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} required />
            </Field>
            {exclude.isError && <ErrorNote error={exclude.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={closeExclude}>
                Cancel
              </Button>
              <Button type="submit" disabled={exclude.isPending || !reason.trim()}>
                {exclude.isPending ? 'Excluding…' : 'Exclude camera'}
              </Button>
            </div>
          </form>
        )}
      </Modal>

      <Modal
        open={canExclude && releasing !== null}
        title={
          releasing
            ? `${hasReleaseFailures(releasing) ? 'Retry release' : 'Release preservation'} for ${releasing.request_number}`
            : ''
        }
        onClose={closeRelease}
        dismissible={!release.isPending}
      >
        {releasing && (
          <form onSubmit={submitRelease} className="space-y-4">
            <p className="text-sm text-ink-muted">
              Releasing deletes every Aware bookmark on this hold. The footage then
              ages out on each camera's normal schedule and cannot be recovered.
            </p>
            <Field
              label="Reason"
              hint={reasonHint('say why the footage no longer needs to be kept')}
            >
              <Textarea
                rows={3}
                value={releaseReason}
                onChange={(e) => setReleaseReason(e.target.value)}
                minLength={MIN_REASON_LENGTH}
                maxLength={MAX_REASON_LENGTH}
                required
              />
            </Field>
            {reasonShortfall(releaseReason) && (
              <p className="text-sm text-ink-muted">{reasonShortfall(releaseReason)}</p>
            )}
            <label className="flex items-start gap-2 text-sm text-ink">
              <input
                type="checkbox"
                checked={attested}
                onChange={(e) => setAttested(e.target.checked)}
                required
              />
              <span>
                I attest that no litigation hold, pending lawsuit, or reasonably
                anticipated litigation covers this footage.
              </span>
            </label>
            {release.isError && <ErrorNote error={release.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={closeRelease} disabled={release.isPending}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={release.isPending || reasonTooShort(releaseReason) || !attested}
              >
                {release.isPending ? 'Releasing…' : 'Release and delete bookmarks'}
              </Button>
            </div>
          </form>
        )}
      </Modal>

      {canExclude && widening && (
        <WidenDialog
          preservation={widening}
          pending={widen.isPending}
          error={widen.isError ? widen.error : null}
          onClose={closeWiden}
          onSubmit={(widenReason) =>
            widen.mutate({
              preservationId: widening.id,
              requestNumber: widening.request_number,
              reason: widenReason,
            })
          }
        />
      )}
    </>
  )
}
