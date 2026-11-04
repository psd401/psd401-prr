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

const KEY_HELP = [
  ['j / k', 'Next / previous record'],
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
  const [index, setIndex] = useState(0)
  const [selection, setSelection] = useState<string[]>([])
  const [undoStack, setUndoStack] = useState<string[]>([])
  const [status, setStatus] = useState('')
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState('')
  const [showClusters, setShowClusters] = useState(false)
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
      setUndoStack((stack) => [...stack, input.id])
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
      setUndoStack((stack) => [...stack, ...input.ids])
      setStatus(`${input.ids.length} selected records: ${STATE_TEXT[input.state]}.`)
      setSelection([])
      refresh()
    },
  })
  const undo = useMutation({
    mutationFn: (id: string) =>
      api<FlightRecord>(`/api/staff/record-items/${id}/undo-review/`, {
        method: 'POST',
        body: {},
      }),
    onSuccess: (record) => {
      setUndoStack((stack) => stack.slice(0, -1))
      setStatus(`Undone: ${record.original_filename} is ${STATE_TEXT[record.review_state] ?? record.review_state}.`)
      refresh()
    },
  })

  // Reads sit below every mutation so each API literal keeps its own method.
  const list = useQuery({
    queryKey: ['review-flight', number, filter],
    queryFn: () =>
      api<Page<FlightRecord> | FlightRecord[]>(
        `/api/staff/record-items/?request=${encodeURIComponent(number)}${filter === 'unreviewed' ? '&review_state=unreviewed' : ''}`,
        { fetchAllPages: false },
      ),
    placeholderData: (previous) => previous,
  })
  const rows = useMemo(() => (list.data ? unwrap(list.data) : []), [list.data])
  const total = list.data && !Array.isArray(list.data) ? list.data.count : rows.length
  const current = rows[Math.min(index, Math.max(rows.length - 1, 0))] ?? null
  const advisory = useQuery({
    queryKey: ['record-responsiveness-advisory', current?.id],
    queryFn: () => api<AdvisoryLatest>(`/api/staff/record-items/${current!.id}/advisory-responsiveness/`),
    enabled: current !== null,
  })

  // Keep the index inside the list as decided records leave it.
  useEffect(() => {
    if (index > 0 && index >= rows.length) setIndex(Math.max(rows.length - 1, 0))
  }, [index, rows.length])

  // Preload the next record so the next keystroke shows it at once.
  useEffect(() => {
    const next = rows[index + 1]
    if (!next) return
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
    if (selection.length > 0) {
      if (state === 'duplicate') {
        setStatus('A duplicate is decided one record at a time.')
        return
      }
      bulk.mutate({ ids: selection, state })
    } else if (current) {
      single.mutate({ id: current.id, state })
    }
  }, [busy, selection, current, bulk, single])
  const undoLast = useCallback(() => {
    const last = undoStack.at(-1)
    if (!busy && last) undo.mutate(last)
  }, [busy, undoStack, undo])
  const toggleSelected = useCallback(() => {
    if (!current) return
    setSelection((ids) => (ids.includes(current.id) ? ids.filter((id) => id !== current.id) : [...ids, current.id]))
  }, [current])

  const commands = useMemo(() => [
    { label: 'Mark responsive', run: () => decide('responsive') },
    { label: 'Mark non-responsive', run: () => decide('non_responsive') },
    { label: 'Mark duplicate', run: () => decide('duplicate') },
    { label: 'Undo last decision', run: undoLast },
    { label: 'Next record', run: () => setIndex((i) => Math.min(i + 1, rows.length - 1)) },
    { label: 'Previous record', run: () => setIndex((i) => Math.max(i - 1, 0)) },
    { label: 'Select or unselect this record', run: toggleSelected },
    { label: 'Show unreviewed only', run: () => { setFilter('unreviewed'); setIndex(0) } },
    { label: 'Show all records', run: () => { setFilter('all'); setIndex(0) } },
    { label: 'Toggle clusters', run: () => setShowClusters((value) => !value) },
  ], [decide, undoLast, toggleSelected, rows.length])
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
        j: () => setIndex((i) => Math.min(i + 1, rows.length - 1)),
        k: () => setIndex((i) => Math.max(i - 1, 0)),
        r: () => decide('responsive'),
        n: () => decide('non_responsive'),
        d: () => decide('duplicate'),
        u: undoLast,
        x: toggleSelected,
      }
      const run = actions[event.key]
      if (run) {
        event.preventDefault()
        run()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen, rows.length, decide, undoLast, toggleSelected])

  const proposal = advisory.data?.latest?.state === 'complete' ? advisory.data.latest.proposal : null
  const error = single.error ?? bulk.error ?? undo.error
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" variant={filter === 'unreviewed' ? 'primary' : 'secondary'}
          onClick={() => { setFilter('unreviewed'); setIndex(0) }}>Unreviewed</Button>
        <Button type="button" variant={filter === 'all' ? 'primary' : 'secondary'}
          onClick={() => { setFilter('all'); setIndex(0) }}>All records</Button>
        <Button type="button" variant="ghost" onClick={() => setShowClusters((value) => !value)}>
          {showClusters ? 'Hide clusters' : 'Show clusters'}
        </Button>
        <Link
          to={`/requests/${number}/records?record_state=unreviewed`}
          className="text-sm font-semibold text-accent underline-offset-2 hover:underline"
        >
          Review all {total} matching in Records
        </Link>
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
      {list.isSuccess && rows.length === 0 && (
        <Card><EmptyState>No records to review here.</EmptyState></Card>
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
                    <span className="mr-2">{selection.includes(row.id) ? '☑' : '☐'}</span>
                    {row.original_filename}
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
          <Card title={current.original_filename}>
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
            {current.reviewable ? (
              <img
                src={previewUrl(current.id)}
                alt={`Page 1 of ${current.original_filename}`}
                className="max-h-[60vh] w-full rounded-md border border-line object-contain"
              />
            ) : (
              <EmptyState>No preview for this file type. Open it from the Records tab.</EmptyState>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button type="button" disabled={busy} onClick={() => decide('responsive')}>Responsive (r)</Button>
              <Button type="button" variant="secondary" disabled={busy} onClick={() => decide('non_responsive')}>Non-responsive (n)</Button>
              <Button type="button" variant="secondary" disabled={busy} onClick={() => decide('duplicate')}>Duplicate (d)</Button>
              <Button type="button" variant="ghost" disabled={busy || undoStack.length === 0} onClick={undoLast}>Undo (u)</Button>
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
