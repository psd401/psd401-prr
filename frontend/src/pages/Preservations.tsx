// ABOUTME: Footage preservation holds screen — the retention race dashboard.
// ABOUTME: Cards per hold with nested camera-hold table and exclude workflow.
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
  Modal,
  Spinner,
  Table,
  Textarea,
  useToast,
} from '../ui'
import { formatDay } from '../date'

interface CameraHold {
  id: number
  camera_id: string
  camera_name: string
  site: string
  clip_id: string | null
  state: string
  error: string
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
  held_count: number
  camera_holds: CameraHold[]
}

const PRESERVATION_STATES = [
  'pending',
  'held',
  'partial',
  'manual',
  'expired',
  'released',
] as const

const STATE_TONES: Record<string, 'green' | 'amber' | 'red' | 'neutral'> = {
  pending: 'amber',
  held: 'green',
  partial: 'amber',
  manual: 'amber',
  expired: 'red',
  released: 'neutral',
  excluded: 'neutral',
  error: 'red',
  failed: 'red',
}

const STATE_LABELS: Record<string, string> = {
  pending: 'Pending — hold executing',
  held: 'Held',
  partial: 'Held (some cameras failed)',
  manual: 'Manual hold required',
  expired: 'Window outside retention',
  released: 'Released',
}

function StateBadge({ state }: { state: string }) {
  return <Badge tone={STATE_TONES[state] ?? 'neutral'}>{STATE_LABELS[state] ?? state}</Badge>
}

interface ExcludeTarget {
  preservationId: number
  hold: CameraHold
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

  const exclude = useMutation({
    mutationFn: ({ preservationId, holdId, reason }: { preservationId: number; holdId: number; reason: string }) =>
      api<CameraHold>(`/api/staff/preservations/${preservationId}/exclude-camera/`, {
        method: 'POST',
        body: { camera_hold_id: holdId, reason },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['preservations'] })
      toast('success', 'Camera excluded from preservation hold.')
      closeExclude()
    },
  })

  function openExclude(preservationId: number, hold: CameraHold) {
    setExcluding({ preservationId, hold })
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
    exclude.mutate({ preservationId: excluding.preservationId, holdId: excluding.hold.id, reason })
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
            className="capitalize"
            onClick={() => changeStateFilter(s)}
            aria-pressed={activeStateFilter === s}
          >
            {s}
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
                </div>
                <StateBadge state={p.state} />
              </div>

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
                      header: 'Clip',
                      cell: (h: CameraHold) =>
                        h.clip_id ? h.clip_id : <span className="text-ink-muted">—</span>,
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
                            {formatDay(h.excluded_at)} by {h.excluded_by ?? '—'}
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
                        h.excluded_at || !canExclude ? null : (
                          <Button variant="ghost" onClick={() => openExclude(p.id, h)}>
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
                Exclusions are logged with your name and reason — RCW 42.56 audit trail.
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
    </>
  )
}
