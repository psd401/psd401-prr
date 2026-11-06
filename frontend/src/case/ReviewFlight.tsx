// ABOUTME: Keyboard-first Review tab (#46): a record list beside its preview,
// ABOUTME: one key per decision, undo, selection, and a command palette.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import { Badge, Button, Card, EmptyState, ErrorNote, Input, Modal, Spinner } from '../ui'
import { RecordClusters } from './RecordClusters'

interface FlightRecord {
  id: string
  original_filename: string
  review_state: string
  reviewable: boolean
  advisory_suggested_state: string | null
  advisory_confidence: number | null
  email_from?: string
  email_sender_domain?: string
  email_sent_at?: string | null
  /** Same body text, same fingerprint: identical copies fold into one. */
  email_body_sha256?: string
}

interface ReadingText {
  text: string
  truncated: boolean
}

/** A record's name without its file extension: usually the email subject. */
function recordTitle(filename: string) {
  return filename.replace(/\.(pdf|eml|msg|txt|html?|docx?)$/i, '')
}

function sentLabel(value?: string | null) {
  if (!value) return ''
  return new Date(value).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function copyKey(row: FlightRecord) {
  if (!row.email_body_sha256) return row.id
  return [row.email_body_sha256, row.original_filename, row.email_sender_domain ?? ''].join('\u0000')
}

function textUrl(id: string) {
  return `/api/staff/record-items/${id}/text/`
}

interface AdvisoryLatest {
  latest: {
    state: string
    proposal: { suggested_state?: string; confidence?: number; rationale?: string }
  } | null
}

type Decision = 'responsive' | 'non_responsive' | 'duplicate'

const STATE_TEXT: Record<string, string> = {
  unreviewed: 'Unreviewed',
  responsive: 'Responsive',
  non_responsive: 'Non-responsive',
  duplicate: 'Duplicate',
  withheld: 'Withheld',
  uncertain: 'Uncertain',
  superseded: 'Superseded',
}

//: The staff API's page size (REST_FRAMEWORK PAGE_SIZE).
const PAGE_SIZE = 50

const KEY_HELP = [
  ['j / k', 'Next / previous record'],
  ['t', 'Switch between text and page image'],
  ['f', 'Focus mode on or off'],
  ['r', 'Responsive'],
  ['n', 'Non-responsive'],
  ['d', 'Duplicate'],
  ['u', 'Undo your last decision'],
  ['x', 'Select for a bulk decision'],
  ['⌘K', 'Command palette'],
]

function isTyping(target: EventTarget | null) {
  const element = target as HTMLElement | null
  return Boolean(
    element && (element.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)),
  )
}

function previewUrl(id: string) {
  return `/api/staff/record-items/${id}/preview-page/?page=1`
}

export function ReviewFlight({ number }: { number: string }) {
  const qc = useQueryClient()
  const [filter, setFilter] = useState<'unreviewed' | 'all'>('unreviewed')
  const [page, setPage] = useState(1)
  const [index, setIndex] = useState(0)
  const [selection, setSelection] = useState<string[]>([])
  // One entry per decision action: a bulk decision is undone as a whole.
  const [undoStack, setUndoStack] = useState<string[][]>([])
  const [status, setStatus] = useState('')
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState('')
  const [showClusters, setShowClusters] = useState(false)
  const [view, setView] = useState<'text' | 'page'>('text')
  const [focusMode, setFocusMode] = useState(false)
  const listRef = useRef<HTMLUListElement>(null)

  const refresh = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['review-flight', number] })
    qc.invalidateQueries({ queryKey: ['record-items'] })
  }, [qc, number])

  const single = useMutation({
    mutationFn: (input: { id: string; state: Decision }) =>
      api<FlightRecord>(`/api/staff/record-items/${input.id}/review/`, {
        method: 'POST',
        body: { state: input.state },
      }),
    onSuccess: (record, input) => {
      setUndoStack((stack) => [...stack, [input.id]])
      setStatus(`${record.original_filename}: ${STATE_TEXT[input.state]}.`)
      if (filter === 'all') setIndex((current) => current + 1)
      refresh()
    },
  })
  const bulk = useMutation({
    mutationFn: (input: { ids: string[]; state: Decision }) =>
      api<FlightRecord[]>('/api/staff/record-items/bulk-review/', {
        method: 'POST',
        body: { record_item_ids: input.ids, state: input.state },
      }),
    onSuccess: (_records, input) => {
      setUndoStack((stack) => [...stack, input.ids])
      setStatus(`${input.ids.length} records: ${STATE_TEXT[input.state]}.`)
      setSelection([])
      refresh()
    },
  })
  const undo = useMutation({
    mutationFn: async (ids: string[]) => {
      const undone: FlightRecord[] = []
      for (const id of ids) {
        undone.push(await api<FlightRecord>(`/api/staff/record-items/${id}/undo-review/`, {
          method: 'POST',
          body: {},
        }))
      }
      return undone
    },
    onSuccess: (records) => {
      const first = records[0]
      setStatus(records.length === 1 && first
        ? `Undone: ${first.original_filename} is ${STATE_TEXT[first.review_state] ?? first.review_state}.`
        : `Undone: ${records.length} records.`)
    },
    // A refused undo is reported and dropped from the stack either way, so
    // the next u reaches the decision before it instead of retrying forever.
    onSettled: () => {
      setUndoStack((stack) => stack.slice(0, -1))
      refresh()
    },
  })

  // Reads sit below every mutation so each API literal keeps its own method.
  const list = useQuery({
    queryKey: ['review-flight', number, filter, page],
    queryFn: () =>
      api<Page<FlightRecord> | FlightRecord[]>(
        `/api/staff/record-items/?request=${encodeURIComponent(number)}${filter === 'unreviewed' ? '&review_state=unreviewed' : ''}${page > 1 ? `&page=${page}` : ''}`,
        { fetchAllPages: false },
      ),
    placeholderData: (previous) => previous,
  })
  const loaded = useMemo(() => (list.data ? unwrap(list.data) : []), [list.data])
  // Identical copies on this page -- the same body text, subject, and sending
  // domain, such as one newsletter sent to every custodian -- fold under their
  // first copy and are decided together. Anything less than all three stays
  // its own row, so no record is decided without being shown.
  const copiesOf = useMemo(() => {
    const groups = new Map<string, string[]>()
    for (const row of loaded) {
      const key = copyKey(row)
      groups.set(key, [...(groups.get(key) ?? []), row.id])
    }
    return groups
  }, [loaded])
  const rows = useMemo(
    () => loaded.filter((row) => (copiesOf.get(copyKey(row)) ?? [])[0] === row.id),
    [loaded, copiesOf],
  )
  const groupOf = useCallback(
    (row: FlightRecord) => copiesOf.get(copyKey(row)) ?? [row.id],
    [copiesOf],
  )
  // Progress across the whole case, not just this page.
  const everything = useQuery({
    queryKey: ['review-flight', number, 'count'],
    queryFn: () => api<Page<FlightRecord> | FlightRecord[]>(
      `/api/staff/record-items/?request=${encodeURIComponent(number)}`,
      { fetchAllPages: false },
    ),
  })
  const allCount = everything.data && !Array.isArray(everything.data) ? everything.data.count : null
  // While the list is loading another page, or refetching after a decision,
  // the rows on screen are the old ones and the highlighted record may be one
  // already decided. No decision is taken until the list has settled.
  const listSettled = list.isSuccess && !list.isFetching && !list.isPlaceholderData
  const total = list.data && !Array.isArray(list.data) ? list.data.count : loaded.length
  const hasNext = Boolean(list.data && !Array.isArray(list.data) && list.data.next)
  const pageStart = (page - 1) * PAGE_SIZE
  const switchFilter = (next: 'unreviewed' | 'all') => {
    setFilter(next)
    setPage(1)
    setIndex(0)
    // A selection is of records the reviewer was looking at; a new list
    // starts with none.
    setSelection([])
  }
  const moveNext = () => {
    if (index < rows.length - 1) setIndex(index + 1)
    else if (hasNext) { setPage(page + 1); setIndex(0) }
    else if (filter === 'unreviewed' && page > 1) {
      // The unreviewed list shrinks as records are decided, so earlier pages
      // can still hold records; the end of a later page wraps back to them.
      setPage(1)
      setIndex(0)
      setStatus('Back to the first page of what is left to review.')
    }
  }
  const movePrevious = () => {
    if (index > 0) setIndex(index - 1)
    else if (page > 1) { setPage(page - 1); setIndex(PAGE_SIZE - 1) }
  }
  const current = rows[Math.min(index, Math.max(rows.length - 1, 0))] ?? null
  const advisory = useQuery({
    queryKey: ['record-responsiveness-advisory', current?.id],
    queryFn: () => api<AdvisoryLatest>(`/api/staff/record-items/${current!.id}/advisory-responsiveness/`),
    enabled: current !== null,
  })
  const reading = useQuery({
    queryKey: ['record-text', current?.id],
    queryFn: () => api<ReadingText>(textUrl(current!.id)),
    enabled: current !== null,
    staleTime: 5 * 60 * 1000,
  })
  const readingText = typeof reading.data?.text === 'string' ? reading.data.text : ''

  // In the unreviewed view the list shrinks as records are decided, so a
  // later page can come back empty while earlier ones still hold records.
  useEffect(() => {
    if (listSettled && rows.length === 0 && page > 1) {
      setPage(1)
      setIndex(0)
    }
  }, [listSettled, rows.length, page])

  // Keep the index inside the list as decided records leave it.
  useEffect(() => {
    if (index > 0 && index >= rows.length) setIndex(Math.max(rows.length - 1, 0))
  }, [index, rows.length])

  // Preload the next record so the next keystroke shows it at once.
  useEffect(() => {
    const next = rows[index + 1]
    if (!next) return
    // The text is not prefetched: reading it is audited as viewed, and a
    // record nobody opened must not read as viewed.
    if (next.reviewable) new Image().src = previewUrl(next.id)
    qc.prefetchQuery({
      queryKey: ['record-responsiveness-advisory', next.id],
      queryFn: () => api<AdvisoryLatest>(`/api/staff/record-items/${next.id}/advisory-responsiveness/`),
    })
  }, [rows, index, qc])

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus()
  }, [index, current?.id])

  const busy = single.isPending || bulk.isPending || undo.isPending
  const decide = useCallback((state: Decision) => {
    if (busy) return
    if (!listSettled) {
      setStatus('Loading the next records; decide again in a moment.')
      return
    }
    if (selection.length > 0) {
      if (state === 'duplicate') {
        setStatus('A duplicate is decided one record at a time.')
        return
      }
      bulk.mutate({ ids: selection.flatMap((id) => {
        const row = rows.find((candidate) => candidate.id === id)
        return row ? groupOf(row) : [id]
      }), state })
    } else if (current) {
      const copies = groupOf(current)
      if (copies.length > 1) {
        if (state === 'duplicate') {
          setStatus('Identical copies are decided together; open the record list to mark one a duplicate.')
          return
        }
        bulk.mutate({ ids: copies, state })
      } else {
        single.mutate({ id: current.id, state })
      }
    }
  }, [busy, listSettled, selection, current, bulk, single, rows, groupOf])
  const undoLast = useCallback(() => {
    const last = undoStack.at(-1)
    if (!busy && last) undo.mutate(last)
  }, [busy, undoStack, undo])
  const moveNextRef = useRef(moveNext)
  const movePreviousRef = useRef(movePrevious)
  const switchFilterRef = useRef(switchFilter)
  moveNextRef.current = moveNext
  movePreviousRef.current = movePrevious
  switchFilterRef.current = switchFilter
  const toggleSelected = useCallback(() => {
    if (!current) return
    setSelection((ids) => (ids.includes(current.id) ? ids.filter((id) => id !== current.id) : [...ids, current.id]))
  }, [current])

  const commands = useMemo(() => [
    { label: 'Mark responsive', run: () => decide('responsive') },
    { label: 'Mark non-responsive', run: () => decide('non_responsive') },
    { label: 'Mark duplicate', run: () => decide('duplicate') },
    { label: 'Undo last decision', run: undoLast },
    { label: 'Next record', run: () => moveNextRef.current() },
    { label: 'Previous record', run: () => movePreviousRef.current() },
    { label: 'Select or unselect this record', run: toggleSelected },
    { label: 'Show unreviewed only', run: () => switchFilterRef.current('unreviewed') },
    { label: 'Show all records', run: () => switchFilterRef.current('all') },
    { label: 'Toggle clusters', run: () => setShowClusters((value) => !value) },
    { label: 'Switch text and page image', run: () => setView((value) => (value === 'text' ? 'page' : 'text')) },
    { label: 'Focus mode on or off', run: () => setFocusMode((value) => !value) },
  ], [decide, undoLast, toggleSelected])
  const matching = commands.filter((command) =>
    command.label.toLowerCase().includes(paletteQuery.trim().toLowerCase()),
  )

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPaletteQuery('')
        setPaletteOpen(true)
        return
      }
      if (paletteOpen || isTyping(event.target) || event.metaKey || event.ctrlKey || event.altKey) return
      const actions: Record<string, () => void> = {
        j: () => moveNextRef.current(),
        k: () => movePreviousRef.current(),
        r: () => decide('responsive'),
        n: () => decide('non_responsive'),
        d: () => decide('duplicate'),
        u: undoLast,
        x: toggleSelected,
        t: () => setView((value) => (value === 'text' ? 'page' : 'text')),
        f: () => setFocusMode((value) => !value),
        Escape: () => setFocusMode(false),
      }
      const run = actions[event.key]
      if (run) {
        event.preventDefault()
        run()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen, decide, undoLast, toggleSelected])

  const proposal = advisory.data?.latest?.state === 'complete' ? advisory.data.latest.proposal : null
  const error = single.error ?? bulk.error ?? undo.error
  const decided = allCount !== null && filter === 'unreviewed' && listSettled ? allCount - total : null
  const allDone = filter === 'unreviewed' && listSettled && loaded.length === 0 && page === 1
    && (allCount ?? 0) > 0
  const copies = current ? groupOf(current).length : 1
  return (
    <div
      className={focusMode
        ? 'fixed inset-0 z-50 space-y-4 overflow-y-auto bg-surface-canvas p-4 sm:p-8'
        : 'space-y-4'}
      aria-label={focusMode ? 'Focused review' : undefined}
    >
      {decided !== null && allCount !== null && allCount > 0 && (
        <div aria-label="Review progress">
          <div className="mb-1 flex justify-between text-sm text-ink-muted">
            <span>{decided.toLocaleString('en-US')} of {allCount.toLocaleString('en-US')} decided</span>
            <span>{total.toLocaleString('en-US')} left</span>
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={allCount}
            aria-valuenow={decided}
            className="h-2 overflow-hidden rounded-full bg-surface-sunken"
          >
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${(decided / allCount) * 100}%` }} />
          </div>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" variant={filter === 'unreviewed' ? 'primary' : 'secondary'}
          onClick={() => switchFilter('unreviewed')}>Unreviewed</Button>
        <Button type="button" variant={filter === 'all' ? 'primary' : 'secondary'}
          onClick={() => switchFilter('all')}>All records</Button>
        <Button type="button" variant="ghost" onClick={() => setShowClusters((value) => !value)}>
          {showClusters ? 'Hide clusters' : 'Show clusters'}
        </Button>
        <Link
          to={`/requests/${number}/review?view=records&record_state=unreviewed`}
          className="text-sm font-semibold text-accent underline-offset-2 hover:underline"
        >
          Review all {total} matching in Records
        </Link>
        <Button type="button" variant="ghost" onClick={() => setFocusMode((value) => !value)}>
          {focusMode ? 'Exit focus (Esc)' : 'Focus (f)'}
        </Button>
        <span className="text-sm text-ink-muted">⌘K for commands</span>
      </div>
      {showClusters && (
        <RecordClusters
          filters={new URLSearchParams({ request: number, review_state: 'unreviewed' })}
          filterKey={[number, 'flight']}
          clusterBy="sender"
          onClusterBy={() => {}}
          onOpen={(id) => {
            const found = rows.findIndex((row) => row.id === id)
            if (found >= 0) setIndex(found)
          }}
        />
      )}
      <p role="status" aria-live="polite" className="min-h-5 text-sm text-ink">
        {selection.length > 0 && `${selection.length} selected; r or n decides them together. `}
        {status}
      </p>
      {error && <ErrorNote error={error} />}
      {list.isPending && <Spinner />}
      {list.isError && <ErrorNote error={list.error} />}
      {allDone && (
        <Card>
          <div className="py-8 text-center">
            <p className="font-heading text-2xl font-bold text-ink">All done</p>
            <p className="mt-2 font-editorial text-lg text-ink-muted">
              Every record on this case has a decision.
            </p>
            <Link
              to={`/requests/${number}/release`}
              className="nx-btn nx-btn--primary mt-5 inline-flex h-auto min-h-11 px-5 py-2"
            >
              Go to Release
            </Link>
          </div>
        </Card>
      )}
      {list.isSuccess && rows.length === 0 && !allDone && (
        <Card><EmptyState>No records to review here.</EmptyState></Card>
      )}
      {rows.length > 0 && !list.isPlaceholderData && (
        <p className="text-sm text-ink-muted">
          Records {pageStart + 1}–{pageStart + rows.length} of {total}
          {(page > 1 || hasNext) && '; j and k continue onto the next or previous page.'}
        </p>
      )}
      {rows.length > 0 && current && (
        <div className="grid gap-4 lg:grid-cols-[minmax(16rem,22rem)_1fr]">
          <Card>
            <ul ref={listRef} role="listbox" aria-label="Records to review" className="max-h-[70vh] space-y-1 overflow-y-auto">
              {rows.map((row, rowIndex) => {
                const selected = row.id === current.id
                return (
                  <li
                    key={row.id}
                    role="option"
                    aria-selected={selected}
                    tabIndex={selected ? 0 : -1}
                    onClick={() => setIndex(rowIndex)}
                    className={`cursor-pointer rounded-md px-2 py-1.5 text-sm outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ${selected ? 'bg-surface-sunken font-semibold text-ink' : 'text-ink-muted'}`}
                  >
                    <span className="mr-2">{selection.includes(row.id) ? '☑' : '☐'}</span>{' '}
                    {recordTitle(row.original_filename)}{' '}
                    {groupOf(row).length > 1 && (
                      <span className="ml-1 text-xs">×{groupOf(row).length}</span>
                    )}
                    <span className="ml-2 text-xs">
                      {STATE_TEXT[row.review_state] ?? row.review_state}
                      {row.advisory_suggested_state &&
                        ` · AI ${STATE_TEXT[row.advisory_suggested_state] ?? row.advisory_suggested_state}${row.advisory_confidence !== null ? ` ${Math.round(row.advisory_confidence * 100)}%` : ''}`}
                    </span>
                  </li>
                )
              })}
            </ul>
          </Card>
          <Card title={recordTitle(current.original_filename)}>
            <p className="-mt-2 mb-3 text-sm text-ink-muted">
              {[current.email_from || current.email_sender_domain, sentLabel(current.email_sent_at)]
                .filter(Boolean).join(' · ')}
              {copies > 1 && ` · ${copies} identical copies on this page, decided together`}
            </p>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Badge tone={current.review_state === 'unreviewed' ? 'neutral' : 'green'}>
                {STATE_TEXT[current.review_state] ?? current.review_state}
              </Badge>
              {proposal?.suggested_state && (
                <Badge tone="blue">
                  AI suggests {STATE_TEXT[proposal.suggested_state] ?? proposal.suggested_state}
                  {typeof proposal.confidence === 'number' && ` (${Math.round(proposal.confidence * 100)}%)`}
                </Badge>
              )}
            </div>
            {proposal?.rationale && (
              <p className="mb-3 text-sm text-ink" aria-label="AI rationale">{proposal.rationale}</p>
            )}
            {view === 'text' && readingText ? (
              <div
                aria-label="Record text"
                className="max-h-[60vh] overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-surface-raised p-5 font-editorial text-base leading-relaxed text-ink"
              >
                {readingText}
              </div>
            ) : current.reviewable ? (
              <img
                src={previewUrl(current.id)}
                alt={`Page 1 of ${current.original_filename}`}
                className="max-h-[60vh] w-full rounded-md border border-line object-contain"
              />
            ) : (
              <EmptyState>No preview for this file type. Open it from the Records tab.</EmptyState>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button type="button" disabled={busy || !listSettled} onClick={() => decide('responsive')}>Responsive (r)</Button>
              <Button type="button" variant="secondary" disabled={busy || !listSettled} onClick={() => decide('non_responsive')}>Non-responsive (n)</Button>
              <Button type="button" variant="secondary" disabled={busy || !listSettled} onClick={() => decide('duplicate')}>Duplicate (d)</Button>
              <Button type="button" variant="ghost" disabled={busy || undoStack.length === 0} onClick={undoLast}>Undo (u)</Button>
              {readingText && current.reviewable && (
                <Button type="button" variant="ghost" onClick={() => setView((value) => (value === 'text' ? 'page' : 'text'))}>
                  {view === 'text' ? 'Page image (t)' : 'Text (t)'}
                </Button>
              )}
            </div>
          </Card>
        </div>
      )}
      <details className="text-sm text-ink-muted">
        <summary>Keyboard shortcuts</summary>
        <dl className="mt-2 grid grid-cols-[6rem_1fr] gap-1">
          {KEY_HELP.map(([key, text]) => (
            <div key={key} className="contents"><dt className="font-mono">{key}</dt><dd>{text}</dd></div>
          ))}
        </dl>
      </details>
      <Modal open={paletteOpen} title="Commands" onClose={() => setPaletteOpen(false)}>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            const first = matching[0]
            setPaletteOpen(false)
            first?.run()
          }}
        >
          <Input
            aria-label="Search commands"
            autoFocus
            value={paletteQuery}
            onChange={(event) => setPaletteQuery(event.target.value)}
          />
        </form>
        <ul className="mt-3 space-y-1" aria-label="Matching commands">
          {matching.map((command) => (
            <li key={command.label}>
              <button
                type="button"
                className="w-full rounded-md px-2 py-1 text-left text-sm hover:bg-surface-sunken focus-visible:outline-2 focus-visible:outline-focus"
                onClick={() => { setPaletteOpen(false); command.run() }}
              >
                {command.label}
              </button>
            </li>
          ))}
        </ul>
      </Modal>
    </div>
  )
}
