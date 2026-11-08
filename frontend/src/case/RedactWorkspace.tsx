// ABOUTME: The Redact tab's workspace: the case's responsive records beside a
// ABOUTME: large page to draw on, the basis remembered, keys to move and add.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  MIN_REASON_LENGTH,
  Select,
  Spinner,
  Textarea,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
  useToast,
} from '../ui'
import { RecordHandlingPanel, type HandledRecord } from './RecordHandlingPanel'
import {
  normalizedBox,
  pointerPosition,
  tooSmall,
  type Exemption,
  type NormalizedBox,
  type PreviewMetadata,
  type Redaction,
  type RedactionProposal,
} from './redactionCanvas'
import { Region } from './RedactionRegion'

interface RedactRecord extends HandledRecord {
  redaction_count: number
  second_review_required: boolean
  second_reviewed_at: string | null
  redacted_artifact_ready: boolean
}

interface RedactionAdvisory {
  enabled: boolean
  proposals: RedactionProposal[]
}

const ZOOMS = [100, 150, 200]
const BASIS_KEY = 'prr.redact.basis'

function rememberedBasis(): string {
  try {
    return window.localStorage.getItem(BASIS_KEY) ?? ''
  } catch {
    return ''
  }
}

function rememberBasis(value: string) {
  try {
    window.localStorage.setItem(BASIS_KEY, value)
  } catch {
    // A blocked store only means the basis is not remembered next visit.
  }
}

function typingIn(target: EventTarget | null): boolean {
  return target instanceof HTMLElement
    && (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable)
}

function recordTitle(filename: string) {
  return filename.replace(/\.(pdf|eml|msg|txt|html?|docx?)$/i, '')
}

export function RedactWorkspace({ number }: { number: string }) {
  const qc = useQueryClient()
  const toast = useToast()
  const [params, setParams] = useSearchParams()
  const [pageNumber, setPageNumber] = useState(1)
  const [zoom, setZoom] = useState(100)
  const [dragStart, setDragStart] = useState<{ x: number; y: number } | null>(null)
  const [draftBox, setDraftBox] = useState<NormalizedBox | null>(null)
  const [basis, setBasis] = useState(rememberedBasis)
  const [explanation, setExplanation] = useState('')
  const [proposalId, setProposalId] = useState<string | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [removeTarget, setRemoveTarget] = useState<number | null>(null)
  const [removeReason, setRemoveReason] = useState('')
  // Suggestions already accepted, dismissed, or set aside with Esc on this
  // record, so the next one opens instead of the same one again.
  const [passed, setPassed] = useState<string[]>([])

  const list = useQuery({
    queryKey: ['record-items', 'redact', number],
    queryFn: () => api<Page<RedactRecord> | RedactRecord[]>(
      `/api/staff/record-items/?request=${encodeURIComponent(number)}&review_state=responsive`,
    ),
  })
  const records = useMemo(() => (list.data ? unwrap(list.data) : []), [list.data])
  const itemParam = params.get('item') ?? ''
  const index = Math.max(0, records.findIndex((row) => row.id === itemParam))
  const record: RedactRecord | undefined = records[index]
  const recordId = record?.id ?? ''
  const drawable = record?.reviewable !== false

  const metadata = useQuery({
    queryKey: ['record-preview-metadata', recordId],
    queryFn: () => api<PreviewMetadata>(`/api/staff/record-items/${recordId}/preview-metadata/`),
    enabled: recordId !== '' && drawable,
  })
  const exemptions = useQuery({
    queryKey: ['exemptions', 'active', 'record-review'],
    queryFn: () => api<Page<Exemption> | Exemption[]>('/api/staff/exemptions/?active=true'),
  })
  const redactions = useQuery({
    queryKey: ['redactions', recordId],
    queryFn: () => api<Page<Redaction> | Redaction[]>(`/api/staff/redactions/?record_item=${recordId}`),
    enabled: recordId !== '',
  })
  const advisory = useQuery({
    queryKey: ['record-redaction-advisory', recordId],
    queryFn: () => api<RedactionAdvisory>(`/api/staff/record-items/${recordId}/advisory-redactions/`),
    enabled: recordId !== '' && drawable,
  })

  const pageCount = metadata.data?.page_count ?? 0
  const activeExemptions = exemptions.data
    ? unwrap(exemptions.data).filter((row) => row.active)
    : []
  const active = redactions.data
    ? unwrap(redactions.data).filter((row) => row.voided_at === null)
    : []
  const suggestions = (advisory.data?.proposals ?? []).filter((row) => row.decision === null)
  const onPage = active.filter((row) => row.page_number === pageNumber)
  const suggestionsOnPage = suggestions.filter(
    (row) => row.page_number === pageNumber && row.id !== proposalId,
  )
  const secondReviewDone = !record?.second_review_required || Boolean(record.second_reviewed_at)
  const nextSuggestion = suggestions.find((row) => !passed.includes(row.id))

  const clearDraft = useCallback(() => {
    if (proposalId) setPassed((ids) => (ids.includes(proposalId) ? ids : [...ids, proposalId]))
    setDraftBox(null)
    setDragStart(null)
    setProposalId(null)
    setRejectReason('')
    setExplanation('')
  }, [proposalId])

  const select = useCallback((nextIndex: number) => {
    const next = records[nextIndex]
    if (!next) return
    const updated = new URLSearchParams(params)
    updated.set('item', next.id)
    setParams(updated, { replace: true })
    setPageNumber(1)
    setDraftBox(null)
    setDragStart(null)
    setProposalId(null)
    setRejectReason('')
    setExplanation('')
    setRemoveTarget(null)
    setPassed([])
  }, [params, records, setParams])

  function refresh() {
    qc.invalidateQueries({ queryKey: ['redactions', recordId] })
    qc.invalidateQueries({ queryKey: ['record-items'] })
    qc.invalidateQueries({ queryKey: ['redaction-groups', number] })
    qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
    qc.invalidateQueries({ queryKey: ['case-closure-readiness', number] })
  }

  const add = useMutation({
    mutationFn: () => api<Redaction>('/api/staff/redactions/', {
      method: 'POST',
      body: {
        record_item: recordId,
        page_number: pageNumber,
        ...draftBox!,
        exemption: Number(basis),
        explanation,
      },
    }),
    onSuccess: () => {
      refresh()
      clearDraft()
      toast('success', 'Redaction added.')
    },
    onError: (error) => toast('error', error.message),
  })
  const accept = useMutation({
    mutationFn: () => api<RedactionProposal>(`/api/staff/redaction-proposals/${proposalId}/accept/`, {
      method: 'POST',
      body: { page_number: pageNumber, ...draftBox!, exemption: Number(basis), explanation },
    }),
    onSuccess: () => {
      refresh()
      qc.invalidateQueries({ queryKey: ['record-redaction-advisory', recordId] })
      clearDraft()
      toast('success', 'Suggestion accepted.')
    },
    onError: (error) => toast('error', error.message),
  })
  const reject = useMutation({
    mutationFn: () => api<RedactionProposal>(`/api/staff/redaction-proposals/${proposalId}/reject/`, {
      method: 'POST',
      body: { reason: rejectReason },
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['record-redaction-advisory', recordId] })
      qc.invalidateQueries({ queryKey: ['redaction-groups', number] })
      clearDraft()
      toast('success', 'Suggestion dismissed.')
    },
    onError: (error) => toast('error', error.message),
  })
  const remove = useMutation({
    mutationFn: () => api<Redaction>(`/api/staff/redactions/${removeTarget}/void/`, {
      method: 'POST',
      body: { reason: removeReason },
    }),
    onSuccess: () => {
      refresh()
      setRemoveTarget(null)
      setRemoveReason('')
      toast('success', 'Redaction removed.')
    },
    onError: (error) => toast('error', error.message),
  })
  const secondReview = useMutation({
    mutationFn: () => api<RedactRecord>(`/api/staff/record-items/${recordId}/second-review/`, {
      method: 'POST',
      body: {},
    }),
    onSuccess: () => {
      refresh()
      toast('success', 'Second review approved.')
    },
    onError: (error) => toast('error', error.message),
  })
  const build = useMutation({
    mutationFn: () => api<RedactRecord>(`/api/staff/record-items/${recordId}/render-redactions/`, {
      method: 'POST',
      body: {},
    }),
    onSuccess: () => {
      refresh()
      toast('success', 'Redacted PDF built.')
    },
    onError: (error) => toast('error', error.message),
  })

  // A remembered basis that is no longer active shows as unchosen, so it must
  // not count as chosen either.
  const basisChosen = activeExemptions.some((row) => String(row.id) === basis)
  const canAdd = Boolean(draftBox) && basisChosen && !add.isPending && !accept.isPending

  const submit = useCallback(() => {
    if (!canAdd) return
    if (proposalId) accept.mutate()
    else add.mutate()
  }, [accept, add, canAdd, proposalId])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (event.key === 'Enter') {
        const target = event.target
        if (target instanceof HTMLTextAreaElement || target instanceof HTMLButtonElement) return
        if (target instanceof HTMLAnchorElement) return
        if (!canAdd) return
        event.preventDefault()
        submit()
        return
      }
      if (typingIn(event.target)) return
      if (event.key === 'j') select(Math.min(records.length - 1, index + 1))
      else if (event.key === 'k') select(Math.max(0, index - 1))
      else if (event.key === 'ArrowRight' && pageNumber < pageCount) {
        setPageNumber((value) => value + 1)
        setDraftBox(null)
        setProposalId(null)
      } else if (event.key === 'ArrowLeft' && pageNumber > 1) {
        setPageNumber((value) => value - 1)
        setDraftBox(null)
        setProposalId(null)
      } else if (event.key === 'x' && proposalId) {
        document.getElementById('dismiss-reason')?.focus()
      } else if (event.key === 'Escape') {
        clearDraft()
      } else {
        return
      }
      event.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canAdd, clearDraft, index, pageCount, pageNumber, proposalId, records.length, select, submit])

  // The AI's boxes come first: with nothing in hand, the next suggestion
  // opens on its page, ready for Enter (accept) or x (dismiss with a reason).
  useEffect(() => {
    if (proposalId || draftBox || dragStart || !nextSuggestion) return
    takeSuggestion(nextSuggestion)
  }, [draftBox, dragStart, nextSuggestion, proposalId])

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    // Captured so a release outside the page still ends the drag here.
    event.currentTarget.setPointerCapture?.(event.pointerId)
    const start = pointerPosition(event)
    setProposalId(null)
    setRejectReason('')
    setDragStart(start)
    setDraftBox(normalizedBox(start, start))
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragStart) return
    if (event.buttons === 0) {
      // The button came up somewhere the page never heard about.
      setDragStart(null)
      return
    }
    setDraftBox(normalizedBox(dragStart, pointerPosition(event)))
  }

  function onPointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragStart) return
    const box = normalizedBox(dragStart, pointerPosition(event))
    setDragStart(null)
    setDraftBox(tooSmall(box) ? null : box)
  }

  function takeSuggestion(proposal: RedactionProposal) {
    setProposalId(proposal.id)
    setRejectReason('')
    setPageNumber(proposal.page_number)
    setDraftBox({ x0: proposal.x0, y0: proposal.y0, x1: proposal.x1, y1: proposal.y1 })
    setBasis(String(proposal.exemption))
    setExplanation(proposal.rationale)
  }

  if (list.isPending) return <Spinner label="Loading records…" />
  if (list.isError) return <ErrorNote error={list.error} />
  if (records.length === 0 || !record) {
    return (
      <Card>
        <EmptyState>
          No responsive records yet. Mark records Responsive in Review, then redact them here.
        </EmptyState>
        <div className="mt-3 text-center">
          <Link
            to={`/requests/${number}/review`}
            className="nx-btn nx-btn--secondary inline-flex h-auto min-h-10 px-4 py-2"
          >
            Go to Review
          </Link>
        </div>
      </Card>
    )
  }

  // Page and form side by side from laptop width; the records sit in a strip
  // above them until a large screen (1536px) has room for a third column.
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem] 2xl:grid-cols-[15rem_minmax(0,1fr)_20rem]">
      <nav aria-label="Responsive records" className="min-w-0 lg:col-span-2 2xl:col-span-1">
        <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
          {records.length} responsive {records.length === 1 ? 'record' : 'records'}
        </p>
        <ul role="listbox" aria-label="Records to redact" className="flex gap-2 overflow-x-auto pb-1 2xl:block 2xl:max-h-[78vh] 2xl:space-y-1 2xl:overflow-x-visible 2xl:overflow-y-auto 2xl:pb-0">
          {records.map((row, rowIndex) => (
            <li
              key={row.id}
              role="option"
              aria-selected={rowIndex === index}
              tabIndex={-1}
              onClick={() => select(rowIndex)}
              className={`w-56 shrink-0 cursor-pointer rounded-lg border px-3 py-2 text-sm 2xl:w-auto ${
                rowIndex === index
                  ? 'border-flow bg-flow-soft text-ink'
                  : 'border-transparent text-ink hover:border-line'
              }`}
            >
              <span className="block truncate font-semibold">{recordTitle(row.original_filename)}</span>
              <span className="text-xs text-ink-muted">
                {row.redaction_count > 0
                  ? `${row.redaction_count} ${row.redaction_count === 1 ? 'redaction' : 'redactions'}`
                  : 'No redactions'}
                {row.redacted_artifact_ready ? ' · PDF built' : ''}
              </span>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-ink-muted">j / k next and previous record</p>
      </nav>

      <section className="min-w-0 space-y-3" aria-label="Page">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="min-w-0 truncate font-heading text-lg font-bold text-ink">
            {recordTitle(record.original_filename)}
          </h2>
          {drawable && pageCount > 0 && (
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                aria-label="Previous page"
                disabled={pageNumber <= 1}
                onClick={() => { setPageNumber((value) => value - 1); setDraftBox(null); setProposalId(null) }}
              >
                ←
              </Button>
              <span className="text-sm text-ink">Page {pageNumber} of {pageCount}</span>
              <Button
                variant="ghost"
                aria-label="Next page"
                disabled={pageNumber >= pageCount}
                onClick={() => { setPageNumber((value) => value + 1); setDraftBox(null); setProposalId(null) }}
              >
                →
              </Button>
              <span className="mx-2 h-5 w-px bg-line" aria-hidden="true" />
              <Button
                variant="ghost"
                aria-label="Zoom out"
                disabled={zoom === ZOOMS[0]}
                onClick={() => setZoom(ZOOMS[Math.max(0, ZOOMS.indexOf(zoom) - 1)] ?? zoom)}
              >
                −
              </Button>
              <span className="w-12 text-center text-sm text-ink">{zoom}%</span>
              <Button
                variant="ghost"
                aria-label="Zoom in"
                disabled={zoom === ZOOMS[ZOOMS.length - 1]}
                onClick={() => setZoom(ZOOMS[Math.min(ZOOMS.length - 1, ZOOMS.indexOf(zoom) + 1)] ?? zoom)}
              >
                +
              </Button>
            </div>
          )}
        </div>

        {!drawable && <RecordHandlingPanel record={record} canReview />}
        {drawable && metadata.isPending && <Spinner label="Opening the page…" />}
        {drawable && metadata.isError && <ErrorNote error={metadata.error} />}
        {drawable && metadata.isSuccess && (
          <div className="max-h-[80vh] overflow-auto rounded-xl border border-line bg-surface-sunken p-3">
            <div
              aria-label={`Draw redactions on page ${pageNumber}`}
              className="relative mx-auto cursor-crosshair touch-none bg-surface-raised shadow-sm"
              style={{ width: `${zoom}%` }}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={() => { setDragStart(null); setDraftBox(null) }}
            >
              <img
                draggable={false}
                className="pointer-events-none block w-full select-none"
                src={`/api/staff/record-items/${record.id}/preview-page/?page=${pageNumber}`}
                alt={`Page ${pageNumber} of ${pageCount}`}
              />
              {onPage.map((row) => <Region key={row.id} box={row} />)}
              {suggestionsOnPage.map((row) => <Region key={row.id} box={row} proposal />)}
              {draftBox && <Region box={draftBox} draft />}
            </div>
          </div>
        )}
        {drawable && (
          <p className="text-xs text-ink-muted">
            Drag over what to hide. Amber is your draft, black is saved, blue is suggested.
            ← → turn pages · Enter adds · Esc clears.
          </p>
        )}
      </section>

      <aside aria-label="Redaction tools" className="space-y-4 lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto">
        {drawable && (
          <section className="space-y-3 rounded-xl border border-line bg-surface-raised p-4">
            <h3 className="font-heading text-base font-bold text-ink">
              {proposalId ? 'Suggested redaction' : 'Add redaction'}
            </h3>
            <Field label="Legal basis">
              <Select
                className="w-full"
                value={basis}
                onChange={(event) => {
                  setBasis(event.target.value)
                  rememberBasis(event.target.value)
                }}
              >
                <option value="">Choose a basis</option>
                {activeExemptions.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.citation} — {row.short_name}
                    {row.requires_second_review ? ' (second review)' : ''}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Note (optional)">
              <Textarea rows={2} value={explanation} onChange={(event) => setExplanation(event.target.value)} />
            </Field>
            {!draftBox && <p className="text-sm text-ink-muted">Drag a box on the page to start.</p>}
            <div className="flex flex-wrap gap-2">
              <Button disabled={!canAdd} onClick={submit}>
                {proposalId ? 'Accept suggestion' : 'Add redaction'}
              </Button>
              {draftBox && <Button variant="ghost" onClick={clearDraft}>Clear</Button>}
            </div>
            {proposalId && (
              <div className="space-y-2 border-t border-line pt-3">
                <Field label="Why dismiss it" hint={reasonHint('why this suggestion is wrong')}>
                  <Textarea
                    id="dismiss-reason"
                    rows={2}
                    minLength={MIN_REASON_LENGTH}
                    value={rejectReason}
                    onChange={(event) => setRejectReason(event.target.value)}
                  />
                </Field>
                {reasonShortfall(rejectReason) && (
                  <p aria-live="polite" className="text-sm text-ink-muted">{reasonShortfall(rejectReason)}</p>
                )}
                <Button
                  variant="secondary"
                  disabled={reasonTooShort(rejectReason) || reject.isPending}
                  onClick={() => reject.mutate()}
                >
                  Dismiss suggestion
                </Button>
              </div>
            )}
          </section>
        )}

        {drawable && suggestions.length > 0 && (
          <section className="space-y-2 rounded-xl border border-info bg-info-soft p-4">
            <h3 className="font-heading text-base font-bold text-ink">
              {suggestions.length} AI {suggestions.length === 1 ? 'suggestion' : 'suggestions'}
            </h3>
            <ul className="max-h-64 space-y-2 overflow-y-auto">
              {suggestions.map((row) => (
                <li key={row.id} className="rounded-lg border border-info bg-surface-raised p-3 text-sm">
                  <p className="font-semibold text-ink">Page {row.page_number} · {row.citation}</p>
                  <p className="mt-1 text-ink">{row.rationale}</p>
                  <Button
                    variant="ghost"
                    className="mt-1"
                    disabled={row.id === proposalId}
                    onClick={() => takeSuggestion(row)}
                  >
                    Use suggestion
                  </Button>
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className="space-y-2 rounded-xl border border-line bg-surface-raised p-4">
          <div className="flex items-center justify-between gap-2">
            <h3 className="font-heading text-base font-bold text-ink">On this record</h3>
            {record.redacted_artifact_ready && <Badge tone="green">PDF built</Badge>}
          </div>
          {redactions.isPending && <Spinner label="Loading redactions…" />}
          {redactions.isError && <ErrorNote error={redactions.error} />}
          {redactions.isSuccess && active.length === 0 && (
            <p className="text-sm text-ink-muted">No redactions yet.</p>
          )}
          <ul className="space-y-2">
            {active.map((row) => (
              <li key={row.id} className="rounded-lg border border-line p-3 text-sm">
                <div className="flex items-start justify-between gap-2">
                  <button
                    type="button"
                    className="text-left"
                    onClick={() => { setPageNumber(row.page_number); setDraftBox(null); setProposalId(null) }}
                  >
                    <span className="block font-semibold text-ink">Page {row.page_number} · {row.citation}</span>
                    <span className="text-ink-muted">{row.exemption_name}</span>
                  </button>
                  <Button
                    variant="ghost"
                    onClick={() => { setRemoveTarget(row.id); setRemoveReason('') }}
                  >
                    Remove
                  </Button>
                </div>
                {row.explanation && <p className="mt-1 text-ink">{row.explanation}</p>}
                {removeTarget === row.id && (
                  <div className="mt-2 space-y-2">
                    <Field label="Why remove it" hint={reasonHint('kept with the record')}>
                      <Textarea
                        rows={2}
                        minLength={MIN_REASON_LENGTH}
                        value={removeReason}
                        onChange={(event) => setRemoveReason(event.target.value)}
                      />
                    </Field>
                    <div className="flex gap-2">
                      <Button
                        variant="danger"
                        disabled={reasonTooShort(removeReason) || remove.isPending}
                        onClick={() => remove.mutate()}
                      >
                        Confirm remove
                      </Button>
                      <Button variant="ghost" onClick={() => setRemoveTarget(null)}>Cancel</Button>
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
          {record.second_review_required && !record.second_reviewed_at && (
            <Button variant="secondary" disabled={secondReview.isPending} onClick={() => secondReview.mutate()}>
              Approve second review
            </Button>
          )}
          {active.length > 0 && secondReviewDone && !record.redacted_artifact_ready && (
            <Button disabled={build.isPending} onClick={() => build.mutate()}>
              Build redacted PDF
            </Button>
          )}
        </section>
      </aside>
    </div>
  )
}
