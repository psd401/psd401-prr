// ABOUTME: The single staff work surface: cross-case messages, statutory backlog, assigned work.
// ABOUTME: Rows carry the case state and the actions, so triage never needs a second screen.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate, useOutletContext, useSearchParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api, unwrap, type Page } from '../api/client'
import {
  CORRESPONDENCE_KIND_LABELS,
  REQUEST_STATUS_EFFECT_LABELS,
  correspondenceBody,
  correspondenceTimestamp,
  type CorrespondenceDetail,
  type CorrespondenceSummary,
} from '../correspondence'
import {
  CASE_SORT_LABELS,
  caseSortParam,
  nextCaseSort,
  parseCaseSort,
} from '../caseSort'
import { AddToExistingCaseModal } from '../case/AddToExistingCaseModal'
import { InboxRow, InboxRowList } from '../inbox/InboxRow'
import { PageHeader } from '../layout/AppLayout'
import { NoActionModal } from '../NoActionModal'
import { CASE_STATUS_CHOICES, statusLabel } from '../caseStatus'
import { statutoryRowLabel, statutoryRowTone } from '../statutory'
import { itemCountLabel, type WorkItemCounts, type WorkItemSummary } from '../work'
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  FilterChip,
  Input,
  MIN_REASON_LENGTH,
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
  reasonHint,
  reasonShortfall,
  reasonTooShort,
  useToast,
} from '../ui'
import {
  InboundCorrespondenceReview,
  type ReviewQueue,
} from './InboundCorrespondenceReview'

/** Chip groups, ordered by what an officer must decide first. */
const VIEW_GROUPS = ['Needs you', 'Your queues', 'In progress', 'Reference'] as const

type ViewGroup = (typeof VIEW_GROUPS)[number]

type InboxView = {
  value: string
  label: string
  group: ViewGroup
  source: 'messages' | 'work' | 'inbound' | 'cases'
  query: string
  empty: string
  description?: string
  countKey?: keyof WorkItemCounts
  /**
   * Rendered as an always-visible chip in the command bar instead of inside
   * the "More views" menu. Chosen by decision loop, not by row count: the bar
   * carries the queues where *not looking* has a consequence.
   *
   * - Needs attention - the shared triage state and default landing queue.
   * - Awaiting statutory response - RCW 42.56.520 is strict liability. A late
   *   acknowledgment is a penalty regardless of intent, so this must never sit
   *   one click further away than anything else.
   * - New - unread. Without it an officer cannot tell a worked queue from an
   *   untouched one.
   * - Unmatched - mail that matched no case. An unmatched records request is
   *   an unanswered one, and no other queue surfaces it.
   * - Assigned to you - personal accountability.
   *
   * Critical is deliberately not primary: it is a severity overlay on work
   * already visible in the queues above. Unassigned and Team are
   * reference-scale, not triage-scale.
   */
  primary?: boolean
}

const OFFICER_VIEWS: InboxView[] = [
  /**
   * The landing view is cases, not messages. A web-form request has no
   * email row anywhere, so a message-first Inbox never showed it at all, and
   * a case that gathered three emails showed three times. One row per open
   * case, flagged when a requester email is waiting that nobody has handled.
   */
  {
    value: 'cases_open',
    label: 'Cases',
    group: 'Needs you',
    source: 'cases',
    query: 'queue=inbox',
    empty: 'No open cases.',
    description: 'Every open case once, whatever channel it arrived by, cases with a new requester email first. Mark caught up clears the flag; it changes nothing on the case.',
    countKey: 'inbox_open_cases',
    primary: true,
  },
  { value: 'needs_attention', label: 'Needs attention', group: 'Needs you', source: 'messages', query: 'work_state=needs_attention', empty: 'No messages need attention.', countKey: 'inbox_needs_attention', primary: true },
  {
    value: 'awaiting_statutory_response',
    label: 'Awaiting statutory response',
    group: 'Needs you',
    source: 'work',
    query: 'inbox_awaiting_statutory_response',
    empty: 'Every received request has a delivered initial response.',
    description: 'Received requests with no delivered five-business-day response under RCW 42.56.520, soonest due first. A draft or an approval does not satisfy the deadline; delivery does.',
    countKey: 'inbox_awaiting_statutory_response',
    primary: true,
  },
  // Unread AND still waiting. A handled row, or one on a closed case, is not
  // news to anyone, however many people never opened it.
  { value: 'new', label: 'Unread', group: 'Needs you', source: 'messages', query: 'read_state=unread&work_state=unresolved', empty: 'No unread messages are waiting.', countKey: 'inbox_new', primary: true },
  { value: 'unmatched', label: 'Needs a case', group: 'Needs you', source: 'inbound', query: 'unmatched', empty: 'Every email has been matched to a case.', countKey: 'inbox_unmatched', primary: true },
  { value: 'quarantined', label: 'Held for review', group: 'Needs you', source: 'inbound', query: 'quarantined', empty: 'No email is held: none was unreadable and no attachment is held by the virus scanner.', countKey: 'inbox_quarantined' },
  { value: 'delivery_exceptions', label: 'Delivery exceptions', group: 'Needs you', source: 'work', query: 'inbox_delivery_exceptions', empty: 'No delivery exceptions.', countKey: 'inbox_delivery_exceptions' },
  {
    value: 'fee_unpaid',
    label: 'Fee unpaid 30 days',
    group: 'Needs you',
    source: 'work',
    query: 'inbox_fee_unpaid',
    empty: 'No fee or deposit has gone unpaid for thirty days.',
    description: 'Cases where an assessed fee or an open deposit has gone unanswered for thirty days. RCW 42.56.120(2) permits closing for non-payment, but nothing here closes anything: send a non-payment closure, waive the fee, or lift the deposit.',
    countKey: 'inbox_fee_unpaid',
  },
  { value: 'waiting', label: 'Replies waiting', group: 'In progress', source: 'messages', query: 'work_state=waiting', empty: 'No messages are waiting.', countKey: 'inbox_waiting' },
  { value: 'received', label: 'Received', group: 'Reference', source: 'messages', query: 'status=received', empty: 'No received messages.' },
  { value: 'handled', label: 'Handled', group: 'Reference', source: 'messages', query: 'work_state=handled', empty: 'No handled messages.', countKey: 'inbox_handled' },
  { value: 'no_action', label: 'No action', group: 'Reference', source: 'messages', query: 'work_state=no_action_required', empty: 'No messages are marked no action.', countKey: 'inbox_no_action' },
  { value: 'all', label: 'All messages', group: 'Reference', source: 'messages', query: '', empty: 'No authorized messages.', countKey: 'inbox_all' },
]

/**
 * The former My Work screen. Its queues span every work kind, not only
 * messages, so they stay a distinct chip group rather than being folded into
 * the message views. They live here because a second work surface that was
 * empty for most officers is what made the shell confusing.
 */
const ASSIGNMENT_VIEWS: InboxView[] = [
  { value: 'personal', label: 'Assigned to you', group: 'Your queues', source: 'work', query: 'personal', empty: 'No work is assigned to you.', description: 'Every kind of unresolved work assigned directly to you.', countKey: 'personal', primary: true },
  { value: 'unassigned', label: 'Unassigned', group: 'Your queues', source: 'work', query: 'unassigned', empty: 'All unresolved work has an owner.', description: 'Unresolved work awaiting an owner.', countKey: 'unassigned' },
  { value: 'team', label: 'Team', group: 'Your queues', source: 'work', query: 'team', empty: 'No unresolved team work.', description: 'All unresolved work visible to your roles.', countKey: 'team' },
  { value: 'critical', label: 'Critical', group: 'Your queues', source: 'work', query: 'critical', empty: 'No statutory or operationally critical work.', description: 'Statutory or operationally critical work across every kind.', countKey: 'critical' },
  { value: 'work_waiting', label: 'Waiting on others', group: 'In progress', source: 'work', query: 'waiting', empty: 'No work is paused on someone else.', description: 'Work paused for another person or event.', countKey: 'waiting' },
  { value: 'recently_changed', label: 'Recently changed', group: 'Reference', source: 'work', query: 'recently_changed', empty: 'No work changed in the past seven days.', description: 'Work changed during the past seven days.', countKey: 'recently_changed' },
]

/**
 * The case index, which used to be its own top-level screen.
 *
 * Every other queue here lists work: a message to triage, a task to finish.
 * This one lists cases. They are the same rows in the same shape, but one row
 * per request rather than one per piece of work, which is what "show me every
 * case" actually means -- a case with five messages is five rows in the other
 * queues and one row here.
 *
 * It carries its own status filter and search because those are case-level
 * questions the work queues cannot answer: "what did we deny", "find the
 * Harborview request". Without them this would be a downgrade from the screen
 * it replaces.
 */
const CASE_VIEWS: [InboxView] = [
  {
    value: 'cases',
    label: 'All cases',
    group: 'Reference',
    source: 'cases',
    query: '',
    empty: 'No cases match these filters.',
    description: 'Every public records case, open by default or any status you choose, soonest statutory due date first.',
    // On the bar, not in the menu. This is the only view with search, and it
    // is the only way to reach a case by number or requester -- burying it
    // behind a disclosure meant staff could not find a case they knew existed
    // and had no way to discover that a search even existed.
    primary: true,
  },
]

const ADMIN_VIEWS = OFFICER_VIEWS.filter((view) => view.value === 'delivery_exceptions')

const LINK_CLASS =
  'inline-flex font-heading text-sm font-semibold text-whulge underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge'

const ROW_CLASS = 'rounded-xl border border-driftwood/60 bg-white p-4 shadow-sm'

/** Views a role may select, most decision-shaped first. */
function inboxViewsFor(roles: StaffRole[]): InboxView[] {
  const isOfficer = roles.includes('records_officer')
  return [
    ...(isOfficer ? OFFICER_VIEWS : roles.includes('admin') ? ADMIN_VIEWS : []),
    ...(isOfficer || roles.includes('liaison') ? ASSIGNMENT_VIEWS : []),
    // Every workflow role gets the case index, including reviewers, who have
    // no message or work queues at all. `scope_case_queryset` decides which
    // cases each role sees and the list serializer blanks requester identity
    // for anyone who is not a Records Officer, so this adds a way in, never
    // a way around.
    ...CASE_VIEWS,
  ]
}

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
  if (!value || !full) return <span className="text-pacific/80">—</span>
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

function timestampLabel(value: string) {
  return new Date(value).toLocaleString('en-US', {
    timeZone: 'America/Los_Angeles',
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function idempotencyKey(action: string) {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `inbox:${action}:${random}`
}

function WorkRows({
  rows,
  empty,
  showSourceContext,
  urgencyLabel,
}: {
  rows: WorkItemSummary[]
  empty: string
  showSourceContext: boolean
  urgencyLabel?: string
}) {
  if (rows.length === 0) return <EmptyState>{empty}</EmptyState>
  return (
    <InboxRowList>
      {rows.map((row) => {
        const due = urgencyLabel ? dayLabel(row.urgency_date) : null
        return (
          <InboxRow
            key={row.id}
            title={row.next_action}
            caseNumber={row.request_number ?? 'No matched case'}
            tags={[
              row.assignee_username ?? 'Unassigned',
              urgencyLabel && due ? `${urgencyLabel}: ${due}` : null,
            ]}
            primaryAction={
              showSourceContext && row.source_url
                ? {
                    label: 'Open case',
                    to: row.source_url,
                    ariaLabel: `Open case ${row.request_number ?? 'context'}`,
                  }
                : null
            }
            secondaryActions={(
              <>
                {showSourceContext && row.request_number
                  && row.work_kind === 'request_action' && (
                  <Link
                    to={`/requests/${row.request_number}/messages?compose=acknowledgment`}
                    className={LINK_CLASS}
                    aria-label={`Prepare acknowledgment for ${row.request_number}`}
                  >
                    Prepare acknowledgment
                  </Link>
                )}
                {/* A per-row accessible name: a list of identical "Work details"
                    links is unusable with a screen reader's link list. */}
                <Link
                  to={row.detail_url}
                  className={LINK_CLASS}
                  aria-label={
                    `Open ${row.next_action} for `
                    + `${row.request_number ?? 'unmatched work'}`
                  }
                >
                  Work details
                </Link>
              </>
            )}
          />
        )
      })}
    </InboxRowList>
  )
}

/**
 * A dialog for closing an auto-created email case that is not a public
 * records request at all. It is deliberately not an auto-dismiss: a machine
 * deciding that an email is not a request is the auto-denial failure mode.
 * A human types why, and the reason lands in the audit trail.
 */
function NonRequestModal({
  open,
  requestNumber,
  subject,
  reason,
  onReasonChange,
  onClose,
  onSubmit,
  pending,
  error,
}: {
  open: boolean
  requestNumber: string
  subject: string
  reason: string
  onReasonChange: (value: string) => void
  onClose: () => void
  onSubmit: () => void
  pending: boolean
  error: unknown
}) {
  const shortfall = reasonShortfall(reason)
  return (
    <Modal
      open={open}
      title="Close as not a records request"
      onClose={onClose}
      dismissible={!pending}
    >
      <form
        className="space-y-4"
        onSubmit={(event: FormEvent) => {
          event.preventDefault()
          onSubmit()
        }}
      >
        <p className="text-sm text-pacific/80">
          {`${requestNumber} was opened automatically from an unmatched email`}
          {subject ? ` (“${subject}”)` : ''}
          . Closing it keeps the message and every attachment as retained
          evidence; nothing is deleted and no requester message is sent.
        </p>
        <Field
          label="Why this email is not a public records request"
          hint={reasonHint('this is recorded in the audit trail')}
        >
          <Textarea
            minLength={MIN_REASON_LENGTH}
            maxLength={2000}
            rows={4}
            required
            value={reason}
            onChange={(event) => onReasonChange(event.target.value)}
          />
        </Field>
        {shortfall && (
          <p aria-live="polite" className="text-sm text-pacific/80">
            {shortfall}
          </p>
        )}
        {error != null && <ErrorNote error={error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button type="submit" disabled={pending || reasonTooShort(reason)}>
            {pending ? 'Closing…' : 'Close as non-request'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

const OVERFLOW_ITEM_CLASS =
  'block w-full rounded-lg px-2 py-1.5 text-left font-heading text-sm text-pacific '
  + 'hover:bg-seafoam focus-visible:outline-2 focus-visible:outline-offset-2 '
  + 'focus-visible:outline-whulge disabled:opacity-50'

/**
 * The row's secondary actions.
 *
 * Native `<details>` rather than a hand-rolled `role="menu"`: it is keyboard
 * operable with no focus-trap or arrow-key code to get wrong, it is already
 * the disclosure pattern used in MessageWorkspace and CorrespondencePanel,
 * and it works on touch. The trigger itself is always rendered and always
 * tab-reachable -- a hover-only reveal would put these controls out of reach
 * of touch and screen-reader users entirely.
 */
function RowOverflow({
  rowName,
  children,
}: {
  rowName: string
  children: ReactNode
}) {
  return (
    <details className="relative">
      <summary
        // Chrome does not consistently expose <summary> as a button, so it can
        // land in the accessibility tree as a generic with no operable role.
        // The explicit role keeps the native keyboard and touch behaviour of
        // <details> while announcing the control as something you can press.
        role="button"
        aria-label={`More actions: ${rowName}`}
        className="inline-flex h-8 w-8 cursor-pointer list-none items-center justify-center rounded-lg border border-driftwood bg-white font-heading text-sm font-bold text-pacific hover:border-whulge focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge [&::-webkit-details-marker]:hidden"
      >
        <span aria-hidden="true">⋯</span>
      </summary>
      <div className="absolute right-0 top-full z-20 mt-1 w-56 rounded-xl border border-driftwood bg-white p-1.5 shadow-lg">
        {children}
      </div>
    </details>
  )
}

/**
 * One triage row. The heading is the message subject, because "Review emailed
 * request" on fourteen identical rows tells an officer nothing about which of
 * them is a public records request and which is a vendor newsletter. The case
 * number, its legal status, and its statutory position sit directly under it,
 * and every disposition an officer needs is on the row itself.
 */
function MessageRow({
  row,
  canAct,
  onChanged,
}: {
  row: CorrespondenceSummary
  canAct: boolean
  onChanged: () => Promise<void>
}) {
  const toast = useToast()
  const work = row.work_item
  // Interpolated separately: an optional chain inside a template literal hides
  // the path from the GUI/agent parity scanner, which reads these calls as the
  // canonical list of what the UI can do.
  const workItemId = work?.id ?? ''
  const context = row.case_context
  const [noActionOpen, setNoActionOpen] = useState(false)
  const [noActionCategory, setNoActionCategory] = useState('informational_only')
  const [noActionReason, setNoActionReason] = useState('')
  const [nonRequestOpen, setNonRequestOpen] = useState(false)
  const [nonRequestReason, setNonRequestReason] = useState('')
  const [addToCaseOpen, setAddToCaseOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)

  /**
   * The list payload carries only a snippet -- the server defers every body
   * column so a 50-row page stays small. Reading the whole message therefore
   * needs the detail endpoint, and it is fetched only once a row is actually
   * opened, which keeps the bounded-list guarantee intact.
   */
  const body = useQuery({
    queryKey: ['inbox-message-body', row.id],
    queryFn: () => api<CorrespondenceDetail>(
      `/api/staff/correspondence/${row.id}/?request=${encodeURIComponent(row.request_number)}`,
    ),
    enabled: expanded,
    staleTime: 60_000,
  })

  const handled = useMutation({
    mutationFn: () => api(`/api/staff/work-items/${workItemId}/disposition/`, {
      method: 'POST',
      body: {
        disposition: 'handled',
        expected_version: work?.version,
        idempotency_key: idempotencyKey('handled'),
      },
    }),
    onSuccess: async () => {
      await onChanged()
      toast('success', 'Marked handled.')
    },
    onError: (error) => toast('error', error.message),
  })

  const noAction = useMutation({
    mutationFn: () => api(`/api/staff/work-items/${workItemId}/no-action-required/`, {
      method: 'POST',
      body: {
        category: noActionCategory,
        reason: noActionReason,
        expected_version: work?.version,
        idempotency_key: idempotencyKey('no-action'),
      },
    }),
    onSuccess: async () => {
      setNoActionOpen(false)
      setNoActionReason('')
      await onChanged()
      toast('success', 'No-action decision recorded.')
    },
    onError: (error) => toast('error', error.message),
  })

  /**
   * Retire the row after a disposition that does not itself retire it.
   *
   * The work item is reconciled from the inbound receipt, and neither closing
   * the case nor moving the message to another one changes what this row is
   * *about* -- so without this the finished message sits in Needs attention
   * forever. But it is bookkeeping stacked on top of a decision the server has
   * already recorded, so it reports failure rather than raising it: an officer
   * told "that failed" about a case that is definitely closed will close it
   * twice. Returns whether the row was actually cleared.
   */
  async function retireHandledWorkItem(key: string) {
    if (!work || work.disposition !== 'needs_attention') return true
    try {
      await api(`/api/staff/work-items/${work.id}/disposition/`, {
        method: 'POST',
        body: {
          disposition: 'handled',
          expected_version: work.version,
          idempotency_key: idempotencyKey(key),
        },
      })
      return true
    } catch {
      return false
    }
  }

  /** What to say when the real work landed but the row would not clear. */
  function retirementNotice(done: string) {
    return `${done}, but this row could not be marked handled. Clear it from `
      + 'the row menu.'
  }

  const closeNonRequest = useMutation({
    mutationFn: async () => {
      await api(
        `/api/staff/requests/${row.request_number}/close-non-request/`,
        { method: 'POST', body: { reason: nonRequestReason } },
      )
      return retireHandledWorkItem('non-request-handled')
    },
    onSuccess: async (retired) => {
      setNonRequestOpen(false)
      setNonRequestReason('')
      await onChanged()
      toast(
        retired ? 'success' : 'error',
        retired
          ? 'Closed as not a records request.'
          : retirementNotice('Closed as not a records request'),
      )
    },
    onError: (error) => toast('error', error.message),
  })

  const busy = handled.isPending || noAction.isPending || closeNonRequest.isPending
  const unresolved = work?.disposition === 'needs_attention'
    || work?.disposition === 'waiting'
  const kindLabel = work?.work_kind === 'email_intake'
    ? 'Emailed request'
    : 'Requester reply'
  const dueLabel = dayLabel(context?.statutory_response_due ?? null)
  const rowName = row.subject.trim() || '(No subject)'

  const statutoryState = context?.statutory_response_state
  const statutoryTone = statutoryState ? statutoryRowTone(statutoryState) : null
  // The one fact that decides whether to open the row keeps a coloured pill;
  // everything else is small plain text at the bottom.
  const loudStatutory = statutoryTone === 'amber' || statutoryTone === 'red'

  return (
    <InboxRow
      title={rowName}
      detail={row.snippet.trim() || null}
      expanded={expanded}
      onToggleExpanded={() => setExpanded((open) => !open)}
      expandedContent={(
        <>
          {body.isPending && <Spinner label="Loading message…" />}
          {body.isError && <ErrorNote error={body.error} />}
          {body.data && (
            <p className="max-h-96 overflow-y-auto whitespace-pre-wrap text-sm text-pacific/90">
              {correspondenceBody(body.data).trim() || '(No message body.)'}
            </p>
          )}
        </>
      )}
      caseNumber={row.request_number}
      timestamp={timestampLabel(correspondenceTimestamp(row))}
      tags={[
        dueLabel ? `Response due ${dueLabel}` : null,
        kindLabel,
        CORRESPONDENCE_KIND_LABELS[row.kind] ?? row.kind,
        context
          ? (REQUEST_STATUS_EFFECT_LABELS[context.legal_status] ?? context.legal_status)
          : null,
        row.attachment_count > 0
          ? `${row.attachment_count} attachment${row.attachment_count === 1 ? '' : 's'}`
          : null,
        statutoryState && !loudStatutory ? statutoryRowLabel(statutoryState) : null,
      ]}
      alert={
        statutoryState && loudStatutory && statutoryTone
          ? { label: statutoryRowLabel(statutoryState), tone: statutoryTone }
          : null
      }
      primaryAction={{
        // Opens the CASE, because that is what the button says. It used to
        // deep-link to /messages?message=<id>, so "Open case" dropped an
        // officer into the correspondence tab of a case they had not seen yet.
        label: 'Open case',
        to: `/requests/${row.request_number}`,
        ariaLabel: `Open case ${row.request_number}: ${rowName}`,
      }}
      secondaryActions={(
        <>
          {/* Mark handled is the highest-frequency disposition in a triage
              queue, so it stays on the row. */}
          {canAct && work && unresolved && (
            <Button
              variant="ghost"
              disabled={busy}
              aria-label={`Mark handled: ${rowName}`}
              onClick={() => handled.mutate()}
            >
              Mark handled
            </Button>
          )}
          {canAct && work && unresolved && (
            <RowOverflow rowName={rowName}>
              {/* Reasoned, Records-Officer-only decisions, grouped away from
                  the routine one-click disposition above on purpose. */}
              <button
                type="button"
                disabled={busy}
                aria-label={`No action: ${rowName}`}
                onClick={() => {
                  noAction.reset()
                  setNoActionOpen(true)
                }}
                className={OVERFLOW_ITEM_CLASS}
              >
                No action…
              </button>
              {/* The commonest correction on an automatically opened case:
                  the requester was following up on a case that already
                  exists. Sits beside the "not a request at all" decision
                  because an officer reaches this row not knowing yet which
                  of the two it is. */}
              {context?.email_intake_auto_created && row.inbound_receipt_id && (
                <button
                  type="button"
                  disabled={busy}
                  aria-label={`Add to an existing case: ${rowName}`}
                  onClick={() => setAddToCaseOpen(true)}
                  className={OVERFLOW_ITEM_CLASS}
                >
                  Add to an existing case…
                </button>
              )}
              {context?.email_intake_auto_created && (
                <button
                  type="button"
                  disabled={busy}
                  aria-label={`Not a records request: ${rowName}`}
                  onClick={() => {
                    closeNonRequest.reset()
                    setNonRequestReason('')
                    setNonRequestOpen(true)
                  }}
                  className={OVERFLOW_ITEM_CLASS}
                >
                  Not a records request…
                </button>
              )}
              <Link
                to={`/requests/${row.request_number}/messages?message=${row.id}`}
                className={OVERFLOW_ITEM_CLASS}
                aria-label={`Read in context: ${rowName}`}
              >
                Read in context
              </Link>
              <Link
                to={`/work/${work.id}`}
                className={OVERFLOW_ITEM_CLASS}
                aria-label={`Work details: ${rowName}`}
              >
                Work details
              </Link>
            </RowOverflow>
          )}
          {work && !(canAct && unresolved) && (
            <Link
              to={`/work/${work.id}`}
              className={LINK_CLASS}
              aria-label={`Work details: ${rowName}`}
            >
              Work details
            </Link>
          )}
        </>
      )}
    >
      {handled.isError && <div className="mt-3"><ErrorNote error={handled.error} /></div>}
      {/* Mounted only where its trigger exists. A closed dialog still holds its
          text in the DOM, and fifty rows each carrying two of them would put
          fifty copies of another case's number and subject into this page. */}
      {canAct && work && unresolved && (
        <NoActionModal
          open={noActionOpen}
          onClose={() => setNoActionOpen(false)}
          onSubmit={() => noAction.mutate()}
          category={noActionCategory}
          onCategoryChange={setNoActionCategory}
          reason={noActionReason}
          onReasonChange={setNoActionReason}
          pending={noAction.isPending}
          error={noAction.isError ? noAction.error : null}
        />
      )}
      {canAct && addToCaseOpen && (
        <AddToExistingCaseModal
          open
          sourceNumber={row.request_number}
          subject={row.subject.trim()}
          receiptId={row.inbound_receipt_id}
          onClose={() => setAddToCaseOpen(false)}
          onRedirected={async () => {
            const retired = await retireHandledWorkItem(
              'email-intake-redirect-handled',
            )
            setAddToCaseOpen(false)
            await onChanged()
            toast(
              retired ? 'success' : 'error',
              retired
                ? 'Message added to the existing case.'
                : retirementNotice('Message added to the existing case'),
            )
          }}
        />
      )}
      {canAct && context?.email_intake_auto_created && (
        <NonRequestModal
          open={nonRequestOpen}
          requestNumber={row.request_number}
          subject={row.subject.trim()}
          reason={nonRequestReason}
          onReasonChange={setNonRequestReason}
          onClose={() => setNonRequestOpen(false)}
          onSubmit={() => closeNonRequest.mutate()}
          pending={closeNonRequest.isPending}
          error={closeNonRequest.isError ? closeNonRequest.error : null}
        />
      )}
    </InboxRow>
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
}

/**
 * Which cases' messages a message view shows. Open cases by default: a
 * closed case is not Inbox business, but its mail stays one choice away.
 */
function MessageCaseControls({
  caseState,
  onChange,
}: {
  caseState: string
  onChange: (value: string) => void
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2">
      <Select
        aria-label="Case state"
        className="w-auto"
        value={caseState}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="open">Open cases</option>
        <option value="closed">Closed cases</option>
        <option value="all">Every case</option>
      </Select>
    </div>
  )
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
    <span className="ml-1.5 whitespace-nowrap rounded border border-driftwood bg-skylight px-1 py-px font-heading text-[0.625rem] font-bold uppercase tracking-[0.06em] text-pacific/80">
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
        className="block size-2 rounded-full bg-whulge"
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
        <span className="whitespace-nowrap text-xs text-pacific/80">
          Estimate
        </span>
      )}
      {basis === 'district_target' && (
        // Named a target, never a deadline. Nothing in RCW 42.56 requires
        // this date and a row that implied otherwise would be teaching the
        // wrong thing to whoever reads it every day.
        <HoverNote full="District working target: 60 calendar days from the first response. Not a statutory deadline.">
          <span className="whitespace-nowrap text-xs text-pacific/80">
            Target
          </span>
        </HoverNote>
      )}
      {basis === 'ferpa' && (
        <span className="whitespace-nowrap text-xs text-pacific/80">
          FERPA access
        </span>
      )}
    </div>
  )
}

/** The latest internal note, one line of it. */
function NoteCell({ note }: { note: CaseNoteSummary | null }) {
  if (!note) return <span className="text-pacific/80">—</span>
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
            className="size-4 accent-whulge"
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
          className="size-4 accent-whulge"
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
        return (
          <span className="flex items-start">
            <HoverNote full={row.subject} className={`line-clamp-2 ${unread ? 'font-semibold' : ''}`}>
              {row.subject || '—'}
            </HoverNote>
            {row.subject_source === 'ai' && <AdvisoryMark />}
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

  function close() {
    setOpen(false)
    setDrafted(null)
    setReadEach(false)
    setProductionSummary('')
    setSubject('')
    setBody('')
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
        },
      },
    ),
    onSuccess: async (outcome) => {
      setDrafted(outcome)
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
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-driftwood/60 bg-skylight/60 px-3 py-2">
      <p className="text-sm text-pacific/90">
        {selected.length} {selected.length === 1 ? 'case' : 'cases'} selected.
      </p>
      <Button type="button" onClick={() => setOpen(true)}>
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
          {!drafted ? (
            <form
              onSubmit={(event: FormEvent) => {
                event.preventDefault()
                draft.mutate()
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
                </Select>
              </Field>
              {kind === 'completion_closure' ? (
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
                <Button type="submit" disabled={draft.isPending}>
                  {draft.isPending ? 'Drafting…' : 'Create drafts'}
                </Button>
                <Button type="button" variant="ghost" onClick={close}>
                  Cancel
                </Button>
              </div>
            </form>
          ) : (
            <div className="space-y-4">
              <p className="text-sm text-pacific/90">
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
                    className="rounded-lg border border-driftwood/60 p-3"
                  >
                    <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                      {row.request_number}
                    </p>
                    <p className="mt-1 text-sm font-semibold text-pacific">
                      {row.subject}
                    </p>
                    <p className="mt-2 whitespace-pre-wrap text-sm text-pacific/90">
                      {row.body}
                    </p>
                  </li>
                ))}
              </ul>
              {drafted.succeeded.length > 0 && (
                <label className="flex items-start gap-2 text-sm text-pacific">
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

/**
 * The queue selector, as one row instead of a wall.
 *
 * Seventeen equally-weighted chips in four labelled groups pushed the first
 * case row 484px down a 900px viewport - more than half the fold spent on
 * controls before any work appeared. The taxonomy itself was researched and
 * is kept exactly as it was: the same four groups, the same labels, the same
 * counts, the same `?view=` values. Only the container changes. Five decision
 * queues stay visible; the rest move into a menu that still groups them under
 * their original headings.
 */
function QueueBar({
  views,
  selected,
  counts,
  onSelect,
}: {
  views: InboxView[]
  selected: InboxView
  counts: WorkItemCounts | undefined
  onSelect: (value: string) => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const primary = views.filter((candidate) => candidate.primary)
  // The menu exists to tame an eighteen-chip wall, not to hide a short list.
  // A liaison sees seven queues, a reviewer one, an admin two; for them every
  // queue fits on the bar, and burying most of a short list behind a
  // disclosure would cost a click for no space saved.
  const fitsOnOneRow = views.length <= 7
  const shown = fitsOnOneRow
    ? views
    // A role with no primary queues of its own still needs a usable bar.
    : primary.length > 0
      ? primary
      : views.slice(0, 5)
  const shownValues = new Set(shown.map((candidate) => candidate.value))
  const overflow = views.filter((candidate) => !shownValues.has(candidate.value))
  // The selected queue is always legible, even when it lives in the menu.
  const selectedInMenu = overflow.some(
    (candidate) => candidate.value === selected.value,
  )

  function chipCount(candidate: InboxView) {
    return counts && candidate.countKey ? counts[candidate.countKey] : undefined
  }

  function chipCountLabel(candidate: InboxView) {
    const count = chipCount(candidate)
    return count === undefined ? undefined : itemCountLabel(count, candidate.label)
  }

  return (
    <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="Inbox views">
      {shown.map((candidate) => (
        <FilterChip
          key={candidate.value}
          selected={candidate.value === selected.value}
          onClick={() => onSelect(candidate.value)}
          count={chipCount(candidate)}
          countLabel={chipCountLabel(candidate)}
        >
          {candidate.label}
        </FilterChip>
      ))}
      {overflow.length > 0 && (
        <div className="relative">
          <button
            type="button"
            aria-expanded={menuOpen}
            aria-haspopup="true"
            // The trigger shows the selected queue's name and pressed state
            // when that queue lives in the menu -- otherwise the current view
            // would be announced only after opening the menu, and the
            // selected-state signal would be colour alone. The accessible
            // name keeps "More views" in both states so the control stays
            // findable as itself while still naming what is selected.
            aria-label={
              selectedInMenu ? `More views: ${selected.label}` : 'More views'
            }
            aria-pressed={selectedInMenu}
            onClick={() => setMenuOpen((open) => !open)}
            className={
              'inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 font-heading text-sm font-semibold '
              + 'transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge '
              + (selectedInMenu
                ? 'border-cedar bg-cedar text-white'
                : 'border-driftwood bg-white text-pacific hover:border-whulge')
            }
          >
            {selectedInMenu ? selected.label : 'More views'}
            <span aria-hidden="true" className="text-xs">{menuOpen ? '▲' : '▼'}</span>
          </button>
          {menuOpen && (
            <div
              className="absolute left-0 top-full z-20 mt-1 w-72 rounded-xl border border-driftwood bg-white p-2 shadow-lg"
              role="group"
              aria-label="More Inbox views"
            >
              {VIEW_GROUPS.filter((group) =>
                overflow.some((candidate) => candidate.group === group),
              ).map((group) => (
                <div
                  key={group}
                  role="group"
                  aria-label={`Inbox views: ${group}`}
                  className="mb-2 last:mb-0"
                >
                  {/* Full-strength pacific: at 12px, text-pacific/70 on white
                      is below WCAG 2 AA. */}
                  <p className="px-2 py-1 font-heading text-xs font-bold uppercase tracking-[0.08em] text-pacific">
                    {group}
                  </p>
                  {overflow
                    .filter((candidate) => candidate.group === group)
                    .map((candidate) => {
                      const count = chipCount(candidate)
                      return (
                        <button
                          key={candidate.value}
                          type="button"
                          aria-pressed={candidate.value === selected.value}
                          onClick={() => {
                            onSelect(candidate.value)
                            setMenuOpen(false)
                          }}
                          className={
                            'flex w-full items-center justify-between gap-3 rounded-lg px-2 py-1.5 text-left '
                            + 'font-heading text-sm focus-visible:outline-2 focus-visible:outline-offset-2 '
                            + 'focus-visible:outline-whulge '
                            + (candidate.value === selected.value
                              ? 'bg-skylight font-bold text-pacific'
                              : 'text-pacific hover:bg-seafoam')
                          }
                        >
                          <span>{candidate.label}</span>
                          {count !== undefined && (
                            <span
                              className="rounded-full bg-driftwood/50 px-2 text-xs font-semibold text-pacific"
                              aria-label={chipCountLabel(candidate)}
                            >
                              {count}
                            </span>
                          )}
                        </button>
                      )
                    })}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * Non-officer roles get no cross-case message content at all: the summary API
 * blanks the subject and snippet for them, so their rows say only what work
 * exists and where it lives.
 */
function BlindMessageRows({ rows }: { rows: CorrespondenceSummary[] }) {
  return (
    <ol className="space-y-3">
      {rows.map((row) => (
        <li key={row.id} className={ROW_CLASS}>
          <p className="font-heading text-lg font-bold">
            {row.work_item?.work_kind === 'email_intake'
              ? 'Review emailed request'
              : 'Review requester reply'}
          </p>
          <p className="mt-1 text-sm text-pacific/80">{row.request_number}</p>
          <div className="mt-3 flex flex-wrap gap-4">
            <Link
              to={`/requests/${row.request_number}/messages?message=${row.id}`}
              className={LINK_CLASS}
            >
              Open case
            </Link>
          </div>
        </li>
      ))}
    </ol>
  )
}

function MessageRows({
  rows,
  empty,
  showTriage,
  onChanged,
}: {
  rows: CorrespondenceSummary[]
  empty: string
  showTriage: boolean
  onChanged: () => Promise<void>
}) {
  if (rows.length === 0) return <EmptyState>{empty}</EmptyState>
  if (!showTriage) return <BlindMessageRows rows={rows} />
  return (
    <ol className="space-y-3">
      {rows.map((row) => (
        <MessageRow
          key={row.id}
          row={row}
          canAct={showTriage}
          onChanged={onChanged}
        />
      ))}
    </ol>
  )
}

export function Inbox({ roles }: { roles: StaffRole[] }) {
  const counts = useOutletContext<WorkItemCounts | undefined>()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const isOfficer = roles.includes('records_officer')
  const views = inboxViewsFor(roles)
  const requestedView = searchParams.get('view') ?? ''
  // views[0] always exists: every role gets CASE_VIEWS. The last fallback only
  // tells the type checker so.
  const view = views.find((candidate) => candidate.value === requestedView)
    ?? views[0]
    ?? CASE_VIEWS[0]

  function selectView(value: string) {
    const next = new URLSearchParams(searchParams)
    next.set('view', value)
    if (value !== 'unmatched' && value !== 'quarantined') {
      next.delete('inbound_receipt')
    }
    // A sort and a page belong to the list that was on screen. Carrying them
    // into another queue would open it on page four of a result set that
    // never existed, sorted by a column that view does not have.
    next.delete('sort')
    next.delete('page')
    setSearchParams(next)
  }

  async function refreshRows() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['inbox-messages'] }),
      queryClient.invalidateQueries({ queryKey: ['inbox-work'] }),
      queryClient.invalidateQueries({ queryKey: ['inbox-cases'] }),
      queryClient.invalidateQueries({ queryKey: ['work-item-counts'] }),
    ])
  }

  const work = useQuery({
    queryKey: ['inbox-work', view.value],
    queryFn: () => api<Page<WorkItemSummary> | WorkItemSummary[]>(
      `/api/staff/work-items/?queue=${view.query}`,
      { fetchAllPages: false },
    ),
    enabled: view.source === 'work',
  })
  const messageQuery = new URLSearchParams(view.query)
  messageQuery.append('work_kind', 'inbound_reply')
  messageQuery.append('work_kind', 'email_intake')
  const messageCaseState = searchParams.get('case') ?? 'open'
  if (messageCaseState !== 'all') messageQuery.append('case_state', messageCaseState)
  // Case-level filters live in the URL like `?view=`, so a filtered case list
  // is a link an officer can send to a colleague or keep in a bookmark.
  // All cases opens on open cases. "all" is the explicit way to every status.
  const caseStatus = searchParams.get('status') ?? 'open'
  const caseSearch = searchParams.get('q') ?? ''
  const casePage = Math.max(1, Number(searchParams.get('page') ?? '1') || 1)
  const caseSort = parseCaseSort(searchParams.get('sort'))
  const [caseSearchDraft, setCaseSearchDraft] = useState(caseSearch)
  const [selectedCases, setSelectedCases] = useState<Set<string>>(new Set())

  function updateCaseParams(
    next: { status?: string; q?: string; page?: string; sort?: string; case?: string },
  ) {
    const params = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(next)) {
      if (value) params.set(key, value)
      else params.delete(key)
    }
    // Changing a filter returns to the first page: page 3 of the previous
    // result set is usually empty, which reads as "no such case".
    if (next.page === undefined) params.delete('page')
    setSearchParams(params)
  }

  // The view's own query (`queue=inbox` for open cases) comes first so a
  // status or search filter narrows it rather than replacing it.
  const caseQuery = new URLSearchParams(view.source === 'cases' ? view.query : '')
  // Only All cases shows the status control, so only it may apply a status.
  // A status carried over in the URL used to narrow the open-cases view
  // invisibly, down to nothing for "closed".
  const showCaseStatus = view.value === 'cases'
  if (showCaseStatus && caseStatus !== 'all') caseQuery.set('status', caseStatus)
  if (caseSearch) caseQuery.set('search', caseSearch)
  if (casePage > 1) caseQuery.set('page', String(casePage))
  if (caseSort) caseQuery.set('ordering', caseSortParam(caseSort))
  const cases = useQuery({
    queryKey: [
      'inbox-cases', view.value, showCaseStatus ? caseStatus : '', caseSearch, casePage,
      caseSortParam(caseSort),
    ],
    queryFn: () => api<Page<CaseListItem> | CaseListItem[]>(
      `/api/staff/requests/${caseQuery.toString() ? `?${caseQuery.toString()}` : ''}`,
      { fetchAllPages: false },
    ),
    enabled: view.source === 'cases',
  })

  const messages = useQuery({
    queryKey: ['inbox-messages', view.value, messageCaseState],
    queryFn: () => api<Page<CorrespondenceSummary> | CorrespondenceSummary[]>(
      `/api/staff/correspondence/?${messageQuery.toString()}`,
      { fetchAllPages: false },
    ),
    enabled: view.source === 'messages',
  })

  return (
    // Radix tooltips need one provider per screen; the case table's truncated
    // cells are the only thing under here that uses them.
    <HoverNoteProvider>
      <PageHeader
        title="Inbox"
        subtitle={isOfficer
          ? 'Every requester message, statutory response, and assigned task in one place. Full message content and requester identity open inside the authorized case.'
          : roles.includes('liaison')
            ? 'Your assigned casework across every queue, ordered by legal urgency.'
            : 'Sanitized delivery-exception work without requester or message content.'}
      />
      {/* One toolbar band: the queue chips and, on a case view, the case
          filters. They used to stack as three separate blocks with their own
          margins, which cost most of the fold before a row appeared. */}
      <div className="mb-3 rounded-xl border border-driftwood/60 bg-white/60 px-3 pt-3">
        <QueueBar
          views={views}
          selected={view}
          counts={counts}
          onSelect={selectView}
        />
        {view.source === 'cases' && (
          <CaseControls
            status={caseStatus}
            onStatusChange={(value) => updateCaseParams({ status: value })}
            showStatus={showCaseStatus}
            searchDraft={caseSearchDraft}
            onSearchDraftChange={setCaseSearchDraft}
            onSearch={() => updateCaseParams({ q: caseSearchDraft.trim() })}
            onClearSearch={() => {
              setCaseSearchDraft('')
              updateCaseParams({ q: '' })
            }}
            searching={!!caseSearch}
          />
        )}
        {view.source === 'messages' && (
          <MessageCaseControls
            caseState={messageCaseState}
            onChange={(value) => updateCaseParams({ case: value })}
          />
        )}
      </div>
      {view.source === 'cases' && caseSort ? (
        // The view description promises its own order (flagged first, then
        // soonest due). A column sort replaces that order outright, so the
        // description would be false; say what order is on screen instead,
        // and keep the way back to "what is late" one click away.
        <p className="mb-3 text-sm text-pacific/80">
          Sorted by {CASE_SORT_LABELS[caseSort.key]},{' '}
          {caseSort.direction === 'asc' ? 'ascending' : 'descending'}. This
          view's default order is off.{' '}
          <button
            type="button"
            className="font-semibold text-whulge underline decoration-ocean/60 underline-offset-2 focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge"
            onClick={() => updateCaseParams({ sort: '' })}
          >
            Restore default order
          </button>
        </p>
      ) : view.description && (
        <p className="mb-3 text-sm text-pacific/80">{view.description}</p>
      )}
      {view.source === 'work' && work.isPending && <Spinner label="Loading Inbox…" />}
      {view.source === 'work' && work.isError && <ErrorNote error={work.error} />}
      {view.source === 'work' && work.data && (
        <WorkRows
          rows={unwrap(work.data)}
          empty={view.empty}
          showSourceContext={isOfficer}
          urgencyLabel={
            view.value === 'awaiting_statutory_response'
              ? 'Statutory response due'
              : undefined
          }
        />
      )}
      {view.source === 'messages' && messages.isPending && <Spinner label="Loading Inbox…" />}
      {view.source === 'messages' && messages.isError && <ErrorNote error={messages.error} />}
      {view.source === 'messages' && messages.data && (
        <MessageRows
          rows={unwrap(messages.data)}
          empty={view.empty}
          showTriage={isOfficer}
          onChanged={refreshRows}
        />
      )}
      {view.source === 'cases' && (
        <>
          {cases.isPending && <Spinner label="Loading cases…" />}
          {cases.isError && <ErrorNote error={cases.error} />}
          {cases.data && (
            <>
              {isOfficer && (
                <BatchNoticeBar
                  // A batch is the rows the officer can see. A case selected
                  // under another filter, search, or page never rides along.
                  selected={unwrap(cases.data)
                    .map((row) => row.number)
                    .filter((number) => selectedCases.has(number))}
                  onClear={() => setSelectedCases(new Set())}
                  onChanged={refreshRows}
                />
              )}
              <CaseTable
                rows={unwrap(cases.data)}
                empty={view.empty}
                label={view.label}
                triage={isOfficer && view.value === 'cases_open'}
                showRequester={isOfficer}
                onChanged={refreshRows}
                sort={caseSort}
                onSort={(key) => updateCaseParams({
                  // Sorting returns to the first page for the same reason a
                  // filter does: page three of the previous order is a
                  // different set of cases.
                  sort: nextCaseSort(caseSort, key),
                })}
                selection={
                  isOfficer
                    ? {
                        selected: selectedCases,
                        onToggle: (number, checked) =>
                          setSelectedCases((current) => {
                            const next = new Set(current)
                            if (checked) next.add(number)
                            else next.delete(number)
                            return next
                          }),
                        onToggleAll: (numbers, checked) =>
                          setSelectedCases((current) => {
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
              {/* The list is one bounded page. Stating the total and offering
                  the rest is not a nicety here: a truncated case list that
                  looks complete is how a case gets reported as not existing. */}
              <div className="mt-3">
                <Pager
                  count={
                    Array.isArray(cases.data) ? cases.data.length : cases.data.count
                  }
                  page={casePage}
                  pageSize={CASE_PAGE_SIZE}
                  noun="case"
                  onPage={(page) => updateCaseParams({ page: String(page) })}
                />
              </div>
            </>
          )}
        </>
      )}
      {view.source === 'inbound' && (
        <InboundCorrespondenceReview
          enabled={isOfficer}
          queue={view.value as ReviewQueue}
          onQueueChange={selectView}
          showQueueSelector={false}
        />
      )}
    </HoverNoteProvider>
  )
}
