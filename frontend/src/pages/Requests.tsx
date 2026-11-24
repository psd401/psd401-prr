// ABOUTME: Requests: every request in one list, open first, with filters and bulk actions.
// ABOUTME: Replaces the Inbox case views; email that is not on a request lives on Email.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useOutletContext, useSearchParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api, unwrap, type Page } from '../api/client'
import {
  CASE_SORT_LABELS,
  caseSortParam,
  nextCaseSort,
  parseCaseSort,
} from '../caseSort'
import { INTAKE_LABEL_TEXT, type IntakeLabel } from '../case/intakeLabels'
import { PageHeader } from '../layout/AppLayout'
import { CASE_STATUS_CHOICES, statusLabel } from '../caseStatus'
import { type WorkItemCounts } from '../work'
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  FilterChip,
  Input,
  Modal,
  Pager,
  Select,
  DataTable,
  HoverNote,
  HoverNoteProvider,
  RowMenu,
  Spinner,
  Textarea,
  type DataTableColumn,
  type TableSort,
  useToast,
} from '../ui'


const LINK_CLASS =
  'inline-flex font-heading text-sm font-semibold text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus'


/**
 * Format a date-only API value (YYYY-MM-DD). These are already Pacific
 * calendar dates, so they must NOT be run through a timezone conversion:
 * `new Date('2026-08-31')` is UTC midnight and converting that to Pacific
 * would report August 30 and understate a statutory deadline by a day.
 */
function dayLabel(value: string | null) {
  if (!value) return null
  const [year, month, day] = value.slice(0, 10).split('-').map(Number)
  if (!year || !month || !day) return null
  return new Date(year, month - 1, day).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

/**
 * A date for a dense table cell: "Jan 20" in the current year, with the year
 * only when it differs. The full date stays on hover and in `dateTime`, so a
 * shortened cell never hides which year a deadline falls in.
 */
function TableDate({ value }: { value: string | null }) {
  const full = dayLabel(value)
  if (!value || !full) return <span className="text-ink-muted">—</span>
  const [year = NaN, month = NaN, day = NaN] = value.slice(0, 10).split('-').map(Number)
  const short = year === new Date().getFullYear()
    ? new Date(year, month - 1, day).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
    })
    : full
  return (
    <time className="whitespace-nowrap" dateTime={value.slice(0, 10)} title={full}>
      {short}
    </time>
  )
}



/** Mirrors REST_FRAMEWORK["PAGE_SIZE"] in config/settings.py. */
const CASE_PAGE_SIZE = 50

/** What a batch reports back, case by case. */
interface BatchOutcome {
  batch_id: string
  succeeded: {
    request_number: string
    correspondence_id: string
    subject: string
    body: string
  }[]
  failed: { request_number: string; detail: string }[]
}

/** One line of the newest internal note on a case. */
export interface CaseNoteSummary {
  body_snippet: string
  author_username: string | null
  created_at: string | null
}

export interface CaseListItem {
  id: number
  number: string
  status: string
  matter_kind: string
  matter_kind_display: string
  ferpa_access_due: string | null
  /**
   * A one-line label for the case. Never the legal record: the request
   * description is immutable and is not edited to produce this.
   */
  subject: string
  /** Where `subject` came from, so an advisory one can be marked as such. */
  subject_source: 'ai' | 'description'
  received_at: string
  statutory_response_due: string | null
  /** What the case owes an answer by next, and under which rule. */
  next_due: string | null
  next_due_basis: 'statutory' | 'estimate' | 'district_target' | 'ferpa' | null
  /** The newest internal staff note, for the list column. */
  latest_note: CaseNoteSummary | null
  estimated_completion: string | null
  closed_at: string | null
  requester_email: string | null
  requester_name: string | null
  assigned_officer_username: string | null
  initial_response_delivered: boolean
  /** Requester emails waiting on the case; sent only by `?queue=inbox`. */
  inbox_needs_attention?: number
  latest_inbound_at?: string | null
  /** The unanswered intake label (#33), on an officer's Inbox rows only. */
  intake_suggestion?: {
    label: IntakeLabel
    target_case_number: string | null
    confidence: number | null
  } | null
}

/**
 * Case-level controls. The queue chips filter work; these filter cases, which
 * is a different axis: "closed" is not a work state and "needs attention" is
 * not a legal status. Search lives here because it is the only way to reach a
 * case by number or requester, and nothing else in the Inbox offers it.
 */
function CaseControls({
  status,
  onStatusChange,
  showStatus,
  searchDraft,
  onSearchDraftChange,
  onSearch,
  onClearSearch,
  searching,
}: {
  status: string
  onStatusChange: (value: string) => void
  /** The open-cases view is already one status band; only All cases filters. */
  showStatus: boolean
  searchDraft: string
  onSearchDraftChange: (value: string) => void
  onSearch: () => void
  onClearSearch: () => void
  searching: boolean
}) {
  return (
    <form
      className="mb-3 flex flex-wrap items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        onSearch()
      }}
    >
      {showStatus && (
        <Select
          aria-label="Case status"
          className="w-auto"
          value={status}
          onChange={(event) => onStatusChange(event.target.value)}
        >
          {/* The two groups staff actually ask for come first; picking the
              seven closed statuses one at a time is not a filter. */}
          <option value="open">All open</option>
          <option value="closed">All closed</option>
          <option value="all">Every status</option>
          <optgroup label="One status">
            {CASE_STATUS_CHOICES.map((choice) => (
              <option key={choice.value} value={choice.value}>{choice.label}</option>
            ))}
          </optgroup>
        </Select>
      )}
      <Input
        type="search"
        aria-label="Search cases"
        className="min-w-0 flex-1"
        value={searchDraft}
        placeholder="Search by case number, requester, or request text"
        onChange={(event) => onSearchDraftChange(event.target.value)}
      />
      <Button type="submit">Search</Button>
      {searching && (
        <Button variant="secondary" type="button" onClick={onClearSearch}>
          Clear
        </Button>
      )}
    </form>
  )
}

/** Marks a cell whose text the model proposed and no human adopted. */
function AdvisoryMark() {
  return (
    <span className="ml-1.5 whitespace-nowrap rounded border border-line bg-surface-sunken px-1 py-px font-heading text-[0.625rem] font-bold uppercase tracking-[0.06em] text-ink-muted">
      <span className="sr-only">Suggested by AI, advisory only: </span>
      <span aria-hidden="true">AI</span>
    </span>
  )
}

/**
 * The unread dot.
 *
 * New requester mail used to get a "2 NEW" badge and a full-width "Mark caught
 * up" button in a column of their own, which cost the width that Assignee and
 * the due date needed and made every row look like it was shouting. Mail
 * clients solved this a long time ago: the row itself goes bold and carries a
 * dot, and the action lives in the row's menu.
 */
function NewActivityDot({ waiting }: { waiting: number }) {
  const label = waiting === 1
    ? 'One new requester message'
    : `${waiting} new requester messages`
  return (
    <HoverNote full={label} className="inline-flex align-middle">
      <span
        aria-hidden="true"
        className="block size-2 rounded-full bg-accent"
      />
      <span className="sr-only">{label}</span>
    </HoverNote>
  )
}

/** What a case owes an answer by next, and under which rule. */
function DueCell({ row }: { row: CaseListItem }) {
  // Not `statutory_response_due`: that date measures the RCW 42.56.520 first
  // response and is spent once one was delivered. The server says which rule
  // produced this date, so the column never has to guess.
  const basis = row.next_due_basis
  return (
    <div className="flex flex-col items-start gap-1">
      <TableDate value={row.next_due} />
      {basis === 'statutory' && (
        <Badge tone="amber">
          <span aria-hidden="true" className="whitespace-nowrap">5-day</span>
          <span className="sr-only">
            Five-business-day statutory response, not yet sent
          </span>
        </Badge>
      )}
      {basis === 'estimate' && (
        <span className="whitespace-nowrap text-xs text-ink-muted">
          Estimate
        </span>
      )}
      {basis === 'district_target' && (
        // Named a target, never a deadline. Nothing in RCW 42.56 requires
        // this date and a row that implied otherwise would be teaching the
        // wrong thing to whoever reads it every day.
        <HoverNote full="District working target: 60 calendar days from the first response. Not a statutory deadline.">
          <span className="whitespace-nowrap text-xs text-ink-muted">
            Target
          </span>
        </HoverNote>
      )}
      {basis === 'ferpa' && (
        <span className="whitespace-nowrap text-xs text-ink-muted">
          FERPA access
        </span>
      )}
    </div>
  )
}

/** The latest internal note, one line of it. */
function NoteCell({ note }: { note: CaseNoteSummary | null }) {
  if (!note) return <span className="text-ink-muted">—</span>
  const who = note.author_username ?? 'staff'
  const when = note.created_at ? dayLabel(note.created_at.slice(0, 10)) : ''
  return (
    <HoverNote full={`${note.body_snippet} — ${who}${when ? `, ${when}` : ''}`}>
      <span className="line-clamp-2 text-xs">{note.body_snippet}</span>
    </HoverNote>
  )
}

function CaseTable({
  rows,
  empty,
  label,
  triage = false,
  showRequester,
  onChanged,
  sort,
  onSort,
  selection,
}: {
  rows: CaseListItem[]
  empty: string
  label: string
  /** Officer on the open-cases view: show the new-mail dot and its action. */
  triage?: boolean
  /** Requester identity is Records Officer only; the server blanks it too. */
  showRequester: boolean
  onChanged?: () => Promise<void>
  sort: TableSort | null
  onSort: (key: string) => void
  selection?: {
    selected: Set<string>
    onToggle: (number: string, checked: boolean) => void
    onToggleAll: (numbers: string[], checked: boolean) => void
  }
}) {
  const navigate = useNavigate()
  const toast = useToast()
  // One mutation for the whole table rather than one per row: a hook cannot
  // live inside a cell callback, and the row menu needs to reach it.
  const caughtUp = useMutation({
    mutationFn: (number: string) =>
      api(`/api/staff/requests/${number}/inbox-handled/`, {
        method: 'POST',
        body: {},
      }),
    onSuccess: async (_data, number) => {
      await onChanged?.()
      toast('success', `${number} marked caught up.`)
    },
    onError: (error) => toast('error', error.message),
  })
  // Correct on an Inbox row (#33): record the officer's agreement, then open
  // the case with the same prefilled action the case page offers.
  const confirmLabel = useMutation({
    mutationFn: ({ number, label }: { number: string; label: IntakeLabel; target: string | null }) =>
      api(`/api/staff/requests/${number}/advisory-intake/feedback/`, {
        method: 'POST',
        body: { outcome: 'confirmed', final_label: label, reason: '' },
      }),
    onSuccess: (_data, { number, label, target }) => {
      const params = new URLSearchParams({ intake_action: label })
      if (target) params.set('intake_target', target)
      navigate(`/requests/${number}/intake?${params.toString()}`)
    },
    onError: (error) => toast('error', error.message),
  })

  if (rows.length === 0) return <EmptyState>{empty}</EmptyState>

  const pageNumbers = rows.map((row) => row.number)
  const allSelected = !!selection
    && pageNumbers.every((number) => selection.selected.has(number))
  const someSelected = !!selection
    && pageNumbers.some((number) => selection.selected.has(number))

  const columns: DataTableColumn<CaseListItem>[] = []
  if (selection) {
    columns.push({
      id: 'select',
      header: 'Select',
      headerCell: (
        <span className="inline-flex flex-col items-center gap-0.5">
          <input
            type="checkbox"
            className="size-4 accent-action"
            // Select-all covers the rows on screen, which is exactly what a
            // batch acts on. A case under another filter, search, or page is
            // not selected by a control the officer cannot see the effect of.
            aria-label="Select every case on this page for a batch notice"
            checked={allSelected}
            // A fresh callback each render is what keeps `indeterminate` in
            // step with the selection: React re-runs a ref whose identity
            // changed. Memoizing it would freeze the mixed state at mount.
            ref={(node) => {
              if (node) node.indeterminate = someSelected && !allSelected
            }}
            onChange={(event) =>
              selection.onToggleAll(pageNumbers, event.target.checked)}
          />
          {/* Sighted officers need the scope the accessible name states. */}
          <span aria-hidden="true" className="text-[0.65rem] font-normal normal-case tracking-normal">
            Page
          </span>
        </span>
      ),
      cell: (row) => (
        <input
          type="checkbox"
          className="size-4 accent-action"
          aria-label={`Select case ${row.number} for a batch notice`}
          checked={selection.selected.has(row.number)}
          onChange={(event) =>
            selection.onToggle(row.number, event.target.checked)}
        />
      ),
    })
  }
  columns.push({
    id: 'number',
    header: 'Case #',
    sortable: true,
    cell: (row) => {
      const waiting = row.inbox_needs_attention ?? 0
      const unread = triage && waiting > 0
      return (
        <span className="flex items-center gap-1.5">
          {unread && <NewActivityDot waiting={waiting} />}
          <Link
            className={`${LINK_CLASS} whitespace-nowrap ${unread ? 'font-bold' : ''}`}
            to={`/requests/${row.number}`}
            aria-label={`Open case ${row.number}`}
          >
            {row.number}
          </Link>
        </span>
      )
    },
  })
  if (showRequester) {
    columns.push({
      id: 'requester',
      header: 'Requester',
      sortable: true,
      cell: (row) => (
        <HoverNote
          full={row.requester_name || row.requester_email || ''}
          className="block max-w-[8rem] truncate"
        >
          {row.requester_name || row.requester_email || '—'}
        </HoverNote>
      ),
    })
  }
  columns.push(
    {
      id: 'subject',
      header: 'Subject',
      sortable: true,
      grow: true,
      cell: (row) => {
        const unread = triage && (row.inbox_needs_attention ?? 0) > 0
        const suggestion = triage ? row.intake_suggestion : null
        const suggestionText = suggestion
          ? `${INTAKE_LABEL_TEXT[suggestion.label]}${
            suggestion.target_case_number ? ` ${suggestion.target_case_number}` : ''
          }`
          : ''
        return (
          <span className="flex flex-col gap-1">
            <span className="flex items-start">
              <HoverNote full={row.subject} className={`line-clamp-2 ${unread ? 'font-semibold' : ''}`}>
                {row.subject || '—'}
              </HoverNote>
              {row.subject_source === 'ai' && <AdvisoryMark />}
            </span>
            {suggestion && (
              <span className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                <span>
                  AI suggests {suggestionText}
                  {typeof suggestion.confidence === 'number'
                    ? ` · ${Math.round(suggestion.confidence * 100)}%`
                    : ''}
                </span>
                <button
                  type="button"
                  className="font-semibold text-accent underline-offset-2 hover:underline"
                  aria-label={`Correct: ${suggestionText} for ${row.number}`}
                  disabled={confirmLabel.isPending}
                  onClick={(event) => {
                    // The row itself opens the case; this button does more.
                    event.stopPropagation()
                    confirmLabel.mutate({
                      number: row.number,
                      label: suggestion.label,
                      target: suggestion.target_case_number,
                    })
                  }}
                >
                  Correct
                </button>
              </span>
            )}
          </span>
        )
      },
    },
    {
      id: 'status',
      header: 'Status',
      sortable: true,
      cell: (row) => (
        <span className="block min-w-[5.5rem] max-w-[7rem] whitespace-normal">
          {statusLabel(row.status)}
        </span>
      ),
    },
    {
      id: 'received',
      header: 'Received',
      sortable: true,
      cell: (row) => <TableDate value={row.received_at.slice(0, 10)} />,
    },
    {
      id: 'due',
      header: 'Next due',
      sortable: true,
      cell: (row) => <DueCell row={row} />,
    },
  )
  if (!triage) {
    // The triage view answers "what needs me now", where a completion
    // estimate is noise and its width pushed Assignee off a laptop screen.
    // All cases keeps it, along with its sort.
    columns.push({
      id: 'estimated',
      header: 'Estimated',
      sortable: true,
      cell: (row) => <TableDate value={row.estimated_completion} />,
    })
  }
  columns.push(
    {
      id: 'latest_note',
      header: 'Latest note',
      cell: (row) => <NoteCell note={row.latest_note ?? null} />,
    },
    {
      id: 'assignee',
      header: 'Assignee',
      sortable: true,
      cell: (row) => (
        <HoverNote
          full={row.assigned_officer_username ?? ''}
          className="block max-w-[7rem] truncate"
        >
          {row.assigned_officer_username ?? 'Unassigned'}
        </HoverNote>
      ),
    },
  )
  if (triage) {
    columns.push({
      id: 'actions',
      header: 'Actions',
      align: 'right',
      cell: (row) => {
        const waiting = row.inbox_needs_attention ?? 0
        return (
          <RowMenu
            label={`Actions for case ${row.number}`}
            items={[
              {
                label: 'Open case',
                onSelect: () => navigate(`/requests/${row.number}`),
              },
              {
                label: 'Mark caught up',
                // Disposes every waiting email row on the case at once. The
                // emails stay exactly as received and the case's legal status
                // does not move.
                disabled: waiting === 0 || caughtUp.isPending,
                onSelect: () => caughtUp.mutate(row.number),
              },
            ]}
          />
        )
      },
    })
  }
  return (
    <DataTable
      label={label}
      columns={columns}
      rows={rows}
      rowKey={(row) => row.id}
      sort={sort}
      onSort={onSort}
      onRowActivate={(row) => navigate(`/requests/${row.number}`)}
      rowLabel={(row) => `Open case ${row.number}`}
      rowEmphasis={(row) => triage && (row.inbox_needs_attention ?? 0) > 0}
    />
  )
}

/**
 * Draft one notice for many cases, then send what was drafted.
 *
 * Drafting and sending stay two clicks on purpose. A bulk send with no
 * intermediate state would be the one action in this application that reaches
 * fifty requesters before anybody has read a word of it. Here the officer gets
 * fifty real drafts, each rendered against its own case, and a second explicit
 * confirmation before any of them leaves the District. Non-payment closures
 * cannot be drafted this way at all: the amount owed differs per case, and one
 * amount typed once is a wrong number sent fifty times.
 */
function BatchNoticeBar({
  selected,
  onClear,
  onChanged,
}: {
  selected: string[]
  onClear: () => void
  onChanged: () => Promise<void>
}) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState('completion_closure')
  const [productionSummary, setProductionSummary] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [drafted, setDrafted] = useState<BatchOutcome | null>(null)
  const [readEach, setReadEach] = useState(false)
  const [templateName, setTemplateName] = useState<string | null>(null)
  const [denied, setDenied] = useState<{
    closed: string[]
    failed: { request_number: string; detail: string }[]
  } | null>(null)

  // A bot-request denial is drafted from a district denial template. The
  // named ones (e.g. "Denial – bot generated") are listed; the first named
  // one is the sensible default, so the officer usually picks nothing.
  const denialTemplates = useQuery({
    queryKey: ['message-template-names'],
    queryFn: () => api<{ templates: { kind: string; names: string[] }[] }>(
      '/api/staff/automatic-message-templates/',
    ),
    enabled: open && kind === 'denial',
    staleTime: 60_000,
  })
  const denialNames = (
    denialTemplates.data?.templates.find((entry) => entry.kind === 'denial')
      ?.names ?? []
  ).filter((name) => name !== '')
  const chosenTemplate = templateName ?? denialNames[0] ?? ''

  function close() {
    setOpen(false)
    setDrafted(null)
    setReadEach(false)
    setProductionSummary('')
    setSubject('')
    setBody('')
    setTemplateName(null)
    setDenied(null)
  }

  const draft = useMutation({
    mutationFn: () => api<BatchOutcome>(
      '/api/staff/correspondence/batch/closure-drafts/',
      {
        method: 'POST',
        body: {
          request_numbers: selected,
          kind,
          confirmed: true,
          production_summary: productionSummary,
          subject,
          body,
          template_name: kind === 'denial' ? chosenTemplate : '',
        },
      },
    ),
    onSuccess: async (outcome) => {
      setDrafted(outcome)
      await onChanged()
    },
    onError: (error) => toast('error', error.message),
  })

  // A bot-request denial is one decision for the whole selection, so it is
  // one click: each case's letter is drafted from the chosen template and
  // sent at once, and each case closes as denied when its letter goes out.
  const denyAndClose = useMutation({
    mutationFn: async () => {
      const drafts = await api<BatchOutcome>(
        '/api/staff/correspondence/batch/closure-drafts/',
        {
          method: 'POST',
          body: {
            request_numbers: selected,
            kind: 'denial',
            confirmed: true,
            template_name: chosenTemplate,
          },
        },
      )
      const sent = drafts.succeeded.length > 0
        ? await api<BatchOutcome>(
          '/api/staff/correspondence/batch/approve-and-send/',
          {
            method: 'POST',
            body: {
              correspondence_ids: drafts.succeeded.map(
                (row) => row.correspondence_id,
              ),
              confirmed: true,
            },
          },
        )
        : { batch_id: '', succeeded: [], failed: [] }
      return {
        closed: sent.succeeded.map((row) => row.request_number),
        failed: [...drafts.failed, ...sent.failed],
      }
    },
    onSuccess: async (outcome) => {
      setDenied(outcome)
      await onChanged()
    },
    onError: (error) => toast('error', error.message),
  })

  const send = useMutation({
    mutationFn: () => api<BatchOutcome>(
      '/api/staff/correspondence/batch/approve-and-send/',
      {
        method: 'POST',
        body: {
          correspondence_ids: (drafted?.succeeded ?? []).map(
            (row) => row.correspondence_id,
          ),
          confirmed: true,
        },
      },
    ),
    onSuccess: async (outcome) => {
      toast(
        outcome.failed.length > 0 ? 'error' : 'success',
        `Sent ${outcome.succeeded.length} of `
        + `${outcome.succeeded.length + outcome.failed.length} notices.`,
      )
      close()
      onClear()
      await onChanged()
    },
    onError: (error) => toast('error', error.message),
  })

  const catchUp = useMutation({
    mutationFn: async () => {
      // One call per case, in order, so each keeps its own audit entry and a
      // refusal names the case it came from.
      const failed: string[] = []
      for (const number of selected) {
        try {
          await api(`/api/staff/requests/${number}/inbox-handled/`, {
            method: 'POST',
            body: {},
          })
        } catch {
          failed.push(number)
        }
      }
      return failed
    },
    onSuccess: async (failed) => {
      await onChanged()
      if (failed.length === 0) {
        toast('success', `${selected.length} marked caught up.`)
        onClear()
        return
      }
      toast(
        'error',
        `Could not mark ${failed.length} of ${selected.length} caught up: `
        + `${failed.join(', ')}.`,
      )
    },
    onError: (error) => toast('error', error.message),
  })

  if (selected.length === 0) return null

  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface-sunken px-3 py-2">
      <p className="text-sm text-ink">
        {selected.length} {selected.length === 1 ? 'case' : 'cases'} selected.
      </p>
      <Button
        type="button"
        onClick={() => {
          setKind('denial')
          setOpen(true)
        }}
      >
        Deny as bot requests
      </Button>
      <Button type="button" variant="secondary" onClick={() => setOpen(true)}>
        Draft notices for these cases
      </Button>
      {/* The row menu's action, for the whole selection. It loops the same
          per-case endpoint rather than adding a bulk one: marking a case
          caught up is audited per case, and a single call that silently
          half-succeeded would leave no record of which half. */}
      <Button
        type="button"
        variant="secondary"
        disabled={catchUp.isPending}
        onClick={() => catchUp.mutate()}
      >
        Mark caught up
      </Button>
      <Button type="button" variant="ghost" onClick={onClear}>
        Clear selection
      </Button>
      {open && (
        <Modal
          open
          title={`Draft one notice for ${selected.length} ${selected.length === 1 ? 'case' : 'cases'}`}
          onClose={close}
        >
          {denied ? (
            <div className="space-y-4">
              <p className="text-sm text-ink">
                Sent {denied.closed.length} bot-request{' '}
                {denied.closed.length === 1 ? 'denial' : 'denials'}. Each case
                closes as denied when its letter goes out.
              </p>
              {denied.failed.length > 0 && (
                <ul className="space-y-1 text-sm text-rust">
                  {denied.failed.map((row) => (
                    <li key={row.request_number}>
                      {row.request_number}: {row.detail}
                    </li>
                  ))}
                </ul>
              )}
              <Button
                type="button"
                onClick={() => {
                  close()
                  onClear()
                }}
              >
                Done
              </Button>
            </div>
          ) : !drafted ? (
            <form
              onSubmit={(event: FormEvent) => {
                event.preventDefault()
                if (kind === 'denial') denyAndClose.mutate()
                else draft.mutate()
              }}
              className="space-y-4"
            >
              <Field label="Kind">
                <Select
                  value={kind}
                  onChange={(event) => setKind(event.target.value)}
                  className="w-full"
                >
                  <option value="completion_closure">Completion closure</option>
                  <option value="general">General correspondence</option>
                  <option value="denial">
                    Bot-request denial (RCW 42.56.080(3))
                  </option>
                </Select>
              </Field>
              {kind === 'denial' ? (
                <Field
                  label="Denial template"
                  hint="Each case gets its own letter from this template, with RCW 42.56.080(3) recorded as the basis."
                >
                  <Select
                    value={chosenTemplate}
                    onChange={(event) => setTemplateName(event.target.value)}
                    className="w-full"
                  >
                    {denialNames.map((name) => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                    <option value="">District default denial</option>
                  </Select>
                </Field>
              ) : kind === 'completion_closure' ? (
                <Field
                  label="What the District produced"
                  hint="The same sentence goes to every selected case. Each draft is editable before it is sent."
                >
                  <Textarea
                    rows={4}
                    value={productionSummary}
                    onChange={(event) => setProductionSummary(event.target.value)}
                    minLength={20}
                    required
                  />
                </Field>
              ) : (
                <>
                  <Field label="Subject">
                    <Input
                      value={subject}
                      onChange={(event) => setSubject(event.target.value)}
                      required
                    />
                  </Field>
                  <Field label="Message">
                    <Textarea
                      rows={6}
                      value={body}
                      onChange={(event) => setBody(event.target.value)}
                      required
                    />
                  </Field>
                </>
              )}
              <div className="flex gap-3">
                {kind === 'denial' ? (
                  <Button type="submit" disabled={denyAndClose.isPending}>
                    {denyAndClose.isPending
                      ? 'Sending…'
                      : `Deny and close ${selected.length} `
                        + `${selected.length === 1 ? 'case' : 'cases'}`}
                  </Button>
                ) : (
                  <Button type="submit" disabled={draft.isPending}>
                    {draft.isPending ? 'Drafting…' : 'Create drafts'}
                  </Button>
                )}
                <Button type="button" variant="ghost" onClick={close}>
                  Cancel
                </Button>
              </div>
            </form>
          ) : (
            <div className="space-y-4">
              <p className="text-sm text-ink">
                {drafted.succeeded.length}{' '}
                {drafted.succeeded.length === 1 ? 'draft' : 'drafts'} created.
                Nothing has been sent yet.
              </p>
              {drafted.failed.length > 0 && (
                <ul className="space-y-1 text-sm text-rust">
                  {drafted.failed.map((row) => (
                    <li key={row.request_number}>
                      {row.request_number}: {row.detail}
                    </li>
                  ))}
                </ul>
              )}
              {/* Each case's own rendering, not a count: one send covers all
                  of them, so each one has to be readable before it goes. */}
              <ul className="max-h-80 space-y-3 overflow-y-auto">
                {drafted.succeeded.map((row) => (
                  <li
                    key={row.correspondence_id}
                    className="rounded-lg border border-line p-3"
                  >
                    <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                      {row.request_number}
                    </p>
                    <p className="mt-1 text-sm font-semibold text-ink">
                      {row.subject}
                    </p>
                    <p className="mt-2 whitespace-pre-wrap text-sm text-ink">
                      {row.body}
                    </p>
                  </li>
                ))}
              </ul>
              {drafted.succeeded.length > 0 && (
                <label className="flex items-start gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    className="mt-0.5 size-4"
                    checked={readEach}
                    onChange={(event) => setReadEach(event.target.checked)}
                  />
                  I have read each notice above and each is right for its case.
                </label>
              )}
              <div className="flex gap-3">
                <Button
                  type="button"
                  disabled={
                    send.isPending
                    || drafted.succeeded.length === 0
                    || !readEach
                  }
                  onClick={() => send.mutate()}
                >
                  {send.isPending
                    ? 'Sending…'
                    : `Send ${drafted.succeeded.length} `
                      + `${drafted.succeeded.length === 1 ? 'notice' : 'notices'}`}
                </Button>
                <Button type="button" variant="ghost" onClick={close}>
                  Leave them as drafts
                </Button>
              </div>
            </div>
          )}
        </Modal>
      )}
    </div>
  )
}


type Focus = '' | 'response_due' | 'new_email' | 'fee_unpaid' | 'mine'

interface FocusOption {
  value: Focus
  label: string
  countKey?: keyof WorkItemCounts
  officerOnly?: boolean
}

/** What an officer asks of the list, in their words. Counts are open requests. */
const FOCUS_OPTIONS: FocusOption[] = [
  { value: '', label: 'All' },
  {
    value: 'response_due',
    label: '5-day response due',
    countKey: 'inbox_awaiting_statutory_response',
    officerOnly: true,
  },
  { value: 'new_email', label: 'New email', countKey: 'inbox_updated_cases', officerOnly: true },
  { value: 'fee_unpaid', label: 'Unpaid fee', countKey: 'inbox_fee_unpaid', officerOnly: true },
  { value: 'mine', label: 'Assigned to me' },
]

function parseFocus(value: string | null): Focus {
  return FOCUS_OPTIONS.some((option) => option.value === value)
    ? (value as Focus)
    : ''
}

export function Requests({ roles }: { roles: StaffRole[] }) {
  const counts = useOutletContext<WorkItemCounts | undefined>()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const isOfficer = roles.includes('records_officer')
  const focusOptions = FOCUS_OPTIONS.filter((option) => isOfficer || !option.officerOnly)
  const show = searchParams.get('show') ?? 'open'
  const focus = parseFocus(searchParams.get('focus'))
  const search = searchParams.get('q') ?? ''
  const page = Math.max(1, Number(searchParams.get('page') ?? '1') || 1)
  const sort = parseCaseSort(searchParams.get('sort'))
  const [searchDraft, setSearchDraft] = useState(search)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  function updateParams(next: Record<string, string>) {
    const params = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(next)) {
      if (value) params.set(key, value)
      else params.delete(key)
    }
    // A new filter starts on page one: page three of another list is
    // usually empty, which reads as "no such request".
    if (next.page === undefined) params.delete('page')
    if (next.show !== undefined && next.show !== 'open') params.delete('show')
    if (next.show && next.show !== 'open') params.set('show', next.show)
    setSearchParams(params)
  }

  // Open requests come from the triage list (flags requests with new email
  // first); closed and every-status lists come from the plain status filter.
  const query = new URLSearchParams()
  if (show === 'open' && isOfficer) query.set('queue', 'inbox')
  else if (show !== 'all') query.set('status', show)
  if (focus) query.set('focus', focus)
  if (search) query.set('search', search)
  if (page > 1) query.set('page', String(page))
  if (sort) query.set('ordering', caseSortParam(sort))
  const queryString = query.toString()
  const requests = useQuery({
    queryKey: ['requests-list', queryString],
    queryFn: () => api<Page<CaseListItem> | CaseListItem[]>(
      `/api/staff/requests/${queryString ? `?${queryString}` : ''}`,
      { fetchAllPages: false },
    ),
  })

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['requests-list'] }),
      queryClient.invalidateQueries({ queryKey: ['work-item-counts'] }),
    ])
  }

  const rows = requests.data ? unwrap(requests.data) : []
  const total = requests.data
    ? (Array.isArray(requests.data) ? requests.data.length : requests.data.count)
    : 0

  return (
    <HoverNoteProvider>
      <PageHeader title="Requests" />
      <div className="mb-3 rounded-xl border border-line bg-surface-raised px-3 pt-3">
        <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="Show requests">
          {focusOptions.map((option) => {
            const count = show === 'open' && option.countKey
              ? counts?.[option.countKey]
              : undefined
            return (
              <FilterChip
                key={option.value || 'all'}
                selected={option.value === focus}
                onClick={() => updateParams({ focus: option.value })}
                count={typeof count === 'number' ? count : undefined}
                countLabel={typeof count === 'number' ? `${count} open` : undefined}
              >
                {option.label}
              </FilterChip>
            )
          })}
        </div>
        <CaseControls
          status={show}
          onStatusChange={(value) => updateParams({ show: value })}
          showStatus
          searchDraft={searchDraft}
          onSearchDraftChange={setSearchDraft}
          onSearch={() => updateParams({ q: searchDraft.trim() })}
          onClearSearch={() => {
            setSearchDraft('')
            updateParams({ q: '' })
          }}
          searching={!!search}
        />
      </div>
      {sort && (
        <p className="mb-3 text-sm text-ink-muted">
          Sorted by {CASE_SORT_LABELS[sort.key]},{' '}
          {sort.direction === 'asc' ? 'ascending' : 'descending'}.{' '}
          <button
            type="button"
            className="font-semibold text-accent underline decoration-line-strong underline-offset-2 focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            onClick={() => updateParams({ sort: '' })}
          >
            Back to soonest due first
          </button>
        </p>
      )}
      {requests.isPending && <Spinner label="Loading requests…" />}
      {requests.isError && <ErrorNote error={requests.error} />}
      {requests.data && (
        <>
          {isOfficer && (
            <BatchNoticeBar
              // Only requests on screen: one selected under another filter,
              // search, or page never rides along.
              selected={rows
                .map((row) => row.number)
                .filter((number) => selected.has(number))}
              onClear={() => setSelected(new Set())}
              onChanged={refresh}
            />
          )}
          <CaseTable
            rows={rows}
            empty="No requests match these filters."
            label="Requests"
            triage={isOfficer && show === 'open'}
            showRequester={isOfficer}
            onChanged={refresh}
            sort={sort}
            onSort={(key) => updateParams({ sort: nextCaseSort(sort, key) })}
            selection={
              isOfficer
                ? {
                    selected,
                    onToggle: (number, checked) =>
                      setSelected((current) => {
                        const next = new Set(current)
                        if (checked) next.add(number)
                        else next.delete(number)
                        return next
                      }),
                    onToggleAll: (numbers, checked) =>
                      setSelected((current) => {
                        const next = new Set(current)
                        for (const number of numbers) {
                          if (checked) next.add(number)
                          else next.delete(number)
                        }
                        return next
                      }),
                  }
                : undefined
            }
          />
          <div className="mt-3">
            <Pager
              count={total}
              page={page}
              pageSize={CASE_PAGE_SIZE}
              noun="request"
              onPage={(next) => updateParams({ page: String(next) })}
            />
          </div>
        </>
      )}
    </HoverNoteProvider>
  )
}
