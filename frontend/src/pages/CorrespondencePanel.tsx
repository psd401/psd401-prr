// ABOUTME: Request-scoped requester conversation, drafting, delivery, and
// ABOUTME: clarification workspace backed entirely by staff REST primitives.
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api, unwrap, type Page } from '../api/client'
import {
  CORRESPONDENCE_KIND_LABELS,
  CORRESPONDENCE_STATUS_LABELS,
  DELIVERY_STATUS_LABELS,
  REQUEST_STATUS_EFFECT_LABELS,
  recipientTypeLabel,
  scanStatusLabel,
  correspondenceDetailQueryKey,
  correspondenceBody,
  correspondenceStatusTone,
  correspondenceSubject,
  correspondenceTimestamp,
  humanFileSize,
  type ClarificationCycle,
  type CorrespondenceDetail,
  type CorrespondenceKind,
  type CorrespondencePreview,
  type CorrespondenceRecipient,
  type DenialExemptionCitation,
  type DeliveryAttempt,
} from '../correspondence'
import { MessageWorkspace } from '../messages/MessageWorkspace'
import { scopeCountNotice, type LatestVaultCount } from '../scopeCountNotice'
import {
  Badge,
  Button,
  Card,
  ErrorNote,
  Field,
  Input,
  Modal,
  Select,
  Spinner,
  Textarea,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
  useToast,
} from '../ui'
import { invalidateBoundedInfiniteQueries } from '../messages/boundedInfiniteCache'
import {
  STATUTORY_RESPONSE_LABELS,
  outstandingStatutoryStep,
  type StatutoryResponse,
} from '../statutory'

type ComposeKind = Exclude<
  CorrespondenceKind,
  'clarification_response' | 'delivery_status_notification'
>

interface ComposeOption {
  value: ComposeKind
  label: string
  officerOnly?: boolean
}

const COMPOSE_OPTIONS: ComposeOption[] = [
  { value: 'general', label: 'General correspondence' },
  { value: 'acknowledgment', label: 'Acknowledgment' },
  { value: 'submission_receipt', label: 'Submission receipt (courtesy)' },
  { value: 'clarification_request', label: 'Clarification request' },
  {
    value: 'scope_clarification',
    label: 'Scope conversation (count and narrowing options)',
    officerOnly: true,
  },
  { value: 'estimate_revision', label: 'Estimate revision' },
  {
    value: 'fee_estimate',
    label: 'Fee estimate and deposit request',
    officerOnly: true,
  },
  { value: 'installment_notice', label: 'Installment notice', officerOnly: true },
  { value: 'responsive_records_notice', label: 'Responsive records notice' },
  { value: 'closure', label: 'Closure draft', officerOnly: true },
  {
    value: 'no_records_closure',
    label: 'No-records / out-of-retention closure',
    officerOnly: true,
  },
  {
    value: 'completion_closure',
    label: 'Completion closure',
    officerOnly: true,
  },
  {
    value: 'nonpayment_closure',
    label: 'Non-payment closure',
    officerOnly: true,
  },
  {
    value: 'third_party_notice',
    label: 'Third-party notice (RCW 42.56.540)',
    officerOnly: true,
  },
  { value: 'denial', label: 'Denial draft', officerOnly: true },
]

const AUTHORED_KINDS = new Set<ComposeKind>([
  'general',
  'responsive_records_notice',
  'closure',
  'denial',
])

const OFFICER_ONLY_KINDS = new Set<CorrespondenceKind>([
  'fee_estimate',
  'scope_clarification',
  'installment_notice',
  'closure',
  'no_records_closure',
  'completion_closure',
  'nonpayment_closure',
  'third_party_notice',
  'denial',
])

// Kinds whose snapshot is built from structured evidence rather than from
// prose, so there is nothing safe to edit in place: correct one by drafting a
// replacement from the corrected facts.
const IMMUTABLE_STRUCTURED_KINDS = new Set<CorrespondenceKind>([
  'fee_estimate',
  'completion_closure',
  'nonpayment_closure',
  'third_party_notice',
])

const IMMUTABLE_EVIDENCE_KINDS = new Set<CorrespondenceKind>([
  'no_records_closure',
  ...IMMUTABLE_STRUCTURED_KINDS,
])

const DRAFT_ENDPOINTS: Record<ComposeKind, { path: string; method: 'POST' }> = {
  general: {
    path: '/api/staff/correspondence/drafts/general/',
    method: 'POST',
  },
  acknowledgment: {
    path: '/api/staff/correspondence/drafts/acknowledgment/',
    method: 'POST',
  },
  submission_receipt: {
    path: '/api/staff/correspondence/drafts/submission-receipt/',
    method: 'POST',
  },
  clarification_request: {
    path: '/api/staff/correspondence/drafts/clarification/',
    method: 'POST',
  },
  estimate_revision: {
    path: '/api/staff/correspondence/drafts/estimate-revision/',
    method: 'POST',
  },
  scope_clarification: {
    path: '/api/staff/correspondence/drafts/scope-clarification/',
    method: 'POST',
  },
  installment_notice: {
    path: '/api/staff/correspondence/drafts/installment/',
    method: 'POST',
  },
  responsive_records_notice: {
    path: '/api/staff/correspondence/drafts/responsive-records/',
    method: 'POST',
  },
  closure: {
    path: '/api/staff/correspondence/drafts/closure/',
    method: 'POST',
  },
  no_records_closure: {
    path: '/api/staff/correspondence/drafts/no-records-closure/',
    method: 'POST',
  },
  fee_estimate: {
    path: '/api/staff/correspondence/drafts/fee-estimate/',
    method: 'POST',
  },
  completion_closure: {
    path: '/api/staff/correspondence/drafts/completion-closure/',
    method: 'POST',
  },
  nonpayment_closure: {
    path: '/api/staff/correspondence/drafts/nonpayment-closure/',
    method: 'POST',
  },
  third_party_notice: {
    path: '/api/staff/correspondence/drafts/third-party-notice/',
    method: 'POST',
  },
  denial: {
    path: '/api/staff/correspondence/drafts/denial/',
    method: 'POST',
  },
}

function parseAddresses(
  value: string,
  recipientType: CorrespondenceRecipient['recipient_type'],
) {
  return value
    .split(/[\n,;]+/)
    .map((address) => address.trim())
    .filter(Boolean)
    .map((email) => ({
      recipient_type: recipientType,
      email,
      display_name: '',
    }))
}

function recipientInputs(to: string, cc: string, bcc: string) {
  return [
    ...parseAddresses(to, 'to'),
    ...parseAddresses(cc, 'cc'),
    ...parseAddresses(bcc, 'bcc'),
  ]
}

function addressesFor(
  recipients: CorrespondenceRecipient[],
  recipientType: CorrespondenceRecipient['recipient_type'],
) {
  return recipients
    .filter((recipient) => recipient.recipient_type === recipientType)
    .map((recipient) => recipient.email)
    .join(', ')
}

function toggleCitation(
  selected: number[],
  citationId: number,
  checked: boolean,
) {
  if (checked) return [...new Set([...selected, citationId])]
  return selected.filter((id) => id !== citationId)
}

function actorSummary(item: CorrespondenceDetail) {
  if (item.direction === 'inbound') {
    const sender = item.received_sender.trim() || 'Unknown sender'
    const recipients = item.received_recipients
      .map((recipient) => recipient.trim())
      .filter(Boolean)
      .join(', ') || 'unknown recipients'
    return `${sender} → ${recipients}${item.matched_by ? ` · matched by ${item.matched_by}` : ''}`
  }
  const recipients = item.recipients
    .filter((recipient) => recipient.recipient_type === 'to')
    .map((recipient) => recipient.email)
    .join(', ')
  return `District → ${recipients || 'no To recipient'}`
}

function cycleState(cycle: ClarificationCycle) {
  if (cycle.cancelled_at) return 'Clarification cancelled'
  if (cycle.resolved_at) return 'Clarification resolved'
  if (cycle.responded_at) return 'Clarification response received'
  return 'Awaiting clarification response'
}

function DeliveryAttempts({ correspondenceId }: { correspondenceId: string }) {
  const attempts = useQuery({
    queryKey: ['correspondence-attempts', correspondenceId],
    queryFn: () =>
      api<DeliveryAttempt[]>(
        `/api/staff/correspondence/${correspondenceId}/attempts/`,
        { fetchAllPages: false },
      ),
  })

  if (attempts.isPending) return <Spinner label="Loading attempts…" />
  if (attempts.isError) return <ErrorNote error={attempts.error} />
  if (attempts.data.length === 0) {
    return <p className="text-xs text-pacific/80">No delivery attempts recorded.</p>
  }

  return (
    <ol className="space-y-2">
      {attempts.data.map((attempt) => (
        <li
          key={attempt.id}
          className="rounded-md border border-driftwood/40 bg-skylight/60 px-3 py-2 text-xs text-pacific/80"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-heading font-bold text-pacific">
              Attempt {attempt.attempt_number} · {attempt.result_category}
            </span>
            <span>{attempt.result_at?.slice(0, 16).replace('T', ' ') ?? 'In progress'}</span>
          </div>
          <p className="mt-1">
            {attempt.error_category || 'No error category'}
            {attempt.claim_owner ? ` · claimed by ${attempt.claim_owner}` : ''}
          </p>
        </li>
      ))}
    </ol>
  )
}

interface CorrespondenceItemProps {
  item: CorrespondenceDetail
  cycle?: ClarificationCycle
  canDraft: boolean
  canManage: boolean
  onEdit: (item: CorrespondenceDetail) => void
  onPreview: (item: CorrespondenceDetail) => void
  onApprove: (item: CorrespondenceDetail) => void
  onQueue: (item: CorrespondenceDetail) => void
  onCancel: (item: CorrespondenceDetail) => void
  onRetry: (item: CorrespondenceDetail) => void
  onDownload: (attachmentId: string) => void
  busy: boolean
}

function CorrespondenceItem({
  item,
  cycle,
  canDraft,
  canManage,
  onEdit,
  onPreview,
  onApprove,
  onQueue,
  onCancel,
  onRetry,
  onDownload,
  busy,
}: CorrespondenceItemProps) {
  const [showAttempts, setShowAttempts] = useState(false)
  const subject = correspondenceSubject(item) || '(No subject)'
  const body = correspondenceBody(item)
  const canCancel =
    canManage
    && ['approved', 'queued', 'failed'].includes(item.status)
    && (
      item.delivery === null
      || ['pending', 'retryable', 'failed'].includes(item.delivery.status)
    )
  const canRetry =
    canManage
    && item.status === 'failed'
    && item.delivery !== null
    && ['failed', 'retryable'].includes(item.delivery.status)
  const canEdit =
    canDraft
    && item.status === 'draft'
    && (canManage || !OFFICER_ONLY_KINDS.has(item.kind))
    && !IMMUTABLE_EVIDENCE_KINDS.has(item.kind)
  const timestamps = [
    ['Created at', item.created_at],
    ['Approved at', item.approved_at],
    ['Queued at', item.queued_at],
    ['Sent at', item.sent_at],
    ['Received at', item.received_at],
    ['Matched at', item.matched_at],
    ['Failed at', item.failed_at],
    ['Cancelled at', item.cancelled_at],
  ].filter((entry): entry is [string, string] => entry[1] !== null)

  return (
    <article
      aria-label={subject}
      className={`relative rounded-lg border bg-white p-4 shadow-sm ${
        item.direction === 'inbound'
          ? 'ml-0 border-ocean/50 lg:mr-16'
          : 'mr-0 border-seaglass/50 lg:ml-16'
      }`}
    >
      <div
        className={`absolute top-0 h-full w-1 rounded-l-lg ${
          item.direction === 'inbound' ? 'left-0 bg-ocean' : 'right-0 bg-seaglass'
        }`}
      />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <Badge tone={item.direction === 'inbound' ? 'blue' : 'green'}>
              {item.direction === 'inbound' ? 'Incoming' : 'Outbound'}
            </Badge>
            <Badge tone={correspondenceStatusTone(item.status)}>
              {CORRESPONDENCE_STATUS_LABELS[item.status] ?? item.status}
            </Badge>
            {item.automated && <Badge tone="amber">Safety net</Badge>}
          </div>
          <h3 className="text-base text-pacific">{subject}</h3>
          <p className="mt-0.5 text-xs text-pacific/80">
            {CORRESPONDENCE_KIND_LABELS[item.kind]} · {actorSummary(item)}
          </p>
        </div>
        <time className="text-xs text-pacific/80">
          {correspondenceTimestamp(item).slice(0, 16).replace('T', ' ')}
        </time>
      </div>

      <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-pacific/80">
        {timestamps.map(([label, timestamp]) => (
          <div key={label} className="flex gap-1">
            <dt className="font-heading font-bold">{label}</dt>
            <dd>
              <time dateTime={timestamp}>
                {timestamp.slice(0, 16).replace('T', ' ')}
              </time>
            </dd>
          </div>
        ))}
      </dl>

      {body && (
        <div
          role="region"
          aria-label={`${subject} message body`}
          tabIndex={0}
          className="mt-3 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-driftwood/30 bg-skylight/50 p-3 text-sm text-pacific/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge"
        >
          {body}
        </div>
      )}

      {(item.denial_exemption_citations ?? []).length > 0 && (
        <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950">
          <p className="font-heading font-bold">Denial exemption basis</p>
          <ul className="mt-1 list-disc pl-5">
            {item.denial_exemption_citations.map((citation) => (
              <li key={citation.id}>
                {citation.citation} — {citation.short_name}
              </li>
            ))}
          </ul>
        </div>
      )}

      {item.recipients.length > 0 && (
        <dl className="mt-3 grid gap-1 text-xs text-pacific/80 sm:grid-cols-3">
          {(['to', 'cc', 'bcc'] as const).map((recipientType) => {
            const addresses = addressesFor(item.recipients, recipientType)
            if (!addresses) return null
            return (
              <div key={recipientType}>
                <dt className="font-heading font-bold uppercase">{recipientType}</dt>
                <dd className="break-all">{addresses}</dd>
              </div>
            )
          })}
        </dl>
      )}

      {item.attachments.length > 0 && (
        <div className="mt-3 space-y-2 border-t border-driftwood/40 pt-3">
          <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
            Attachments
          </p>
          {item.attachments.map((attachment) => (
            <div
              key={attachment.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-driftwood/40 px-3 py-2"
            >
              <div>
                <p className="text-sm text-pacific">{attachment.original_filename}</p>
                <p className="text-xs text-pacific/80">
                  {humanFileSize(attachment.size)} · {scanStatusLabel(attachment.scan_status)}
                </p>
              </div>
              {attachment.is_downloadable ? (
                <Button
                  variant="ghost"
                  aria-label={`Download attachment ${attachment.original_filename}`}
                  onClick={() => onDownload(attachment.id)}
                  disabled={busy}
                >
                  Download
                </Button>
              ) : (
                <Badge tone={attachment.scan_status === 'infected' ? 'red' : 'amber'}>
                  Unavailable
                </Badge>
              )}
            </div>
          ))}
        </div>
      )}

      {(item.failure_category || item.failure_description) && (
        <div className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900">
          <p className="font-heading font-bold">
            {item.failure_category || 'Delivery failed'}
          </p>
          {item.failure_description && <p className="mt-0.5">{item.failure_description}</p>}
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-pacific/80">
        {item.delivery && (
          <Badge
            tone={
              item.delivery.status === 'sent'
                ? 'green'
                : item.delivery.status === 'failed'
                  || item.delivery.status === 'retryable'
                  ? 'red'
                  : 'blue'
            }
          >
            {DELIVERY_STATUS_LABELS[item.delivery.status] ?? item.delivery.status}
          </Badge>
        )}
        {cycle && <span>{cycleState(cycle)}</span>}
        {item.resulting_status_event_id && (
          <span>
            Request status effect:{' '}
            {item.resulting_status
              ? REQUEST_STATUS_EFFECT_LABELS[item.resulting_status]
                ?? item.resulting_status
              : 'Recorded on the case timeline'}
          </span>
        )}
      </div>

      {canManage
        && item.kind === 'scope_clarification'
        && (item.status === 'draft' || item.status === 'approved') && (
        <div className="mt-3">
          <ScopeCountNotice
            requestNumber={item.request_number}
            matchCount={String(item.draft_metadata.match_count ?? '')}
          />
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-2 border-t border-driftwood/40 pt-3">
        {item.direction === 'outbound' && (
          <Button variant="ghost" onClick={() => onPreview(item)} disabled={busy}>
            Preview exact message
          </Button>
        )}
        {canEdit && (
          <Button variant="ghost" onClick={() => onEdit(item)} disabled={busy}>
            Edit draft
          </Button>
        )}
        {canManage && item.status === 'draft' && (
          <Button onClick={() => onApprove(item)} disabled={busy}>
            Approve and send
          </Button>
        )}
        {/* Approving and sending are one action now, so a draft never stops
            here. Messages approved before that change still exist, and this
            is the only way to release them. */}
        {canManage && item.status === 'approved' && (
          <Button onClick={() => onQueue(item)} disabled={busy}>
            Send message
          </Button>
        )}
        {canRetry && (
          <Button
            onClick={() => onRetry(item)}
            disabled={busy}
            aria-label={`Retry ${subject}`}
          >
            Retry delivery
          </Button>
        )}
        {canCancel && (
          <Button
            variant="danger"
            onClick={() => onCancel(item)}
            disabled={busy}
            aria-label={`Cancel ${subject}`}
          >
            Cancel message
          </Button>
        )}
        {item.delivery && item.delivery.attempt_count > 0 && (
          <Button
            variant="ghost"
            onClick={() => setShowAttempts((visible) => !visible)}
          >
            Delivery attempts
          </Button>
        )}
      </div>

      {showAttempts && (
        <div className="mt-3 border-t border-driftwood/40 pt-3">
          <DeliveryAttempts correspondenceId={item.id} />
        </div>
      )}
    </article>
  )
}

interface SearchStepState {
  location: string
  custodian: string
  method: string
  dateRange: string
  result: string
}

function emptySearchStep(): SearchStepState {
  return {
    location: '',
    custodian: '',
    method: '',
    dateRange: '',
    result: '',
  }
}

interface ComposeState {
  kind: ComposeKind
  subject: string
  body: string
  to: string
  cc: string
  bcc: string
  estimatedCompletion: string
  clarificationQuestions: string
  clarificationDue: string
  newDate: string
  reason: string
  installmentId: string
  isFinal: boolean
  scopeForm: ScopeClarificationForm
  matchCount: string
  querySummary: string
  narrowingOptions: NarrowingOptionState[]
  responseBy: string
  exemptionCitationIds: number[]
  determination: 'no_responsive_records' | 'outside_retention' | 'not_maintained'
  searchCompletedAt: string
  searchSteps: SearchStepState[]
  adequacyReason: string
  retentionScheduleReference: string
  incidentHoldChecked: boolean
  priorExportsChecked: boolean
  emailDriveCopiesChecked: boolean
  otherCopiesChecked: boolean
  counselApprovalReference: string
  productionSummary: string
  estimatedAmount: string
  estimateBasis: string
  depositAmount: string
  amountDue: string
  paymentRequestedOn: string
  employeeName: string
  employeeEmail: string
  attorneyName: string
  attorneyEmail: string
  templateName: string
}

type ScopeClarificationForm = 'informational' | 'clarification'

interface NarrowingOptionState {
  // Stable across removals, so focus and screen readers follow the option
  // the officer was editing rather than its on-screen position.
  id: number
  terms: string
  rationale: string
}

let nextNarrowingOptionId = 0

function newNarrowingOption(terms = '', rationale = ''): NarrowingOptionState {
  nextNarrowingOptionId += 1
  return { id: nextNarrowingOptionId, terms, rationale }
}

const SCOPE_FORM_CONSEQUENCES: Record<ScopeClarificationForm, string> = {
  informational:
    'Tells the requester how large their request is and offers narrower '
    + 'options. The statutory clock keeps running and work continues.',
  clarification:
    'Asks the requester to clarify under RCW 42.56.520(3). On delivery the '
    + 'case enters Clarification and the statutory clock is tolled until they '
    + 'reply.',
}

// Mirrors MAX_NARROWING_OPTIONS in apps/correspondence/automatic_templates.py;
// the server refuses a fifth option either way.
const MAX_NARROWING_OPTIONS = 4

function narrowingOptionPayload(options: NarrowingOptionState[]) {
  return options
    .map((option) => ({
      terms: option.terms
        .split(',')
        .map((term) => term.trim())
        .filter(Boolean),
      rationale: option.rationale.trim(),
    }))
    .filter((option) => option.terms.length > 0 || option.rationale.length > 0)
}

function scopeClarificationPayload(state: {
  scopeForm: ScopeClarificationForm
  matchCount: string
  querySummary: string
  narrowingOptions: NarrowingOptionState[]
  responseBy: string
}) {
  return {
    form: state.scopeForm,
    // Number('') is 0, which would send a blank field as a real count.
    // Anything but a whole number goes up as null and the server refuses it.
    match_count: /^\d+$/.test(state.matchCount.trim())
      ? Number(state.matchCount.trim())
      : null,
    query_summary: state.querySummary,
    narrowing_options: narrowingOptionPayload(state.narrowingOptions),
    response_by: state.responseBy,
  }
}

function narrowingOptionsFromMetadata(value: unknown): NarrowingOptionState[] {
  if (!Array.isArray(value)) return []
  return value.map((option) => {
    const entry = (option ?? {}) as { terms?: unknown; rationale?: unknown }
    return newNarrowingOption(
      Array.isArray(entry.terms) ? entry.terms.join(', ') : '',
      String(entry.rationale ?? ''),
    )
  })
}

interface ScopeClarificationValue {
  scopeForm: ScopeClarificationForm
  matchCount: string
  querySummary: string
  narrowingOptions: NarrowingOptionState[]
  responseBy: string
}

/**
 * Tells a Records Officer when a scope letter's count and the request's latest
 * Vault count disagree. It never blocks: the officer may have a reason for the
 * number, and the district chose to inform rather than refuse.
 */
function ScopeCountNotice({
  requestNumber,
  matchCount,
}: {
  requestNumber: string
  matchCount: string
}) {
  // Same key and endpoint as the Collection tab, so both share one cache.
  const latest = useQuery({
    queryKey: ['vault-counts', requestNumber],
    queryFn: () => api<{ latest: LatestVaultCount | null }>(
      `/api/staff/requests/${requestNumber}/vault-counts/`,
    ),
  })
  const notice = scopeCountNotice(matchCount, latest.data?.latest)
  if (!notice) return null
  return (
    <p
      role="status"
      className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
    >
      {notice}
    </p>
  )
}

/**
 * The scope conversation's fields, shared by the compose and edit dialogs so
 * the officer cannot be shown two different versions of the one choice that
 * matters here: whether this letter asks for clarification, and therefore
 * stops the District's clock, or merely reports what the search found.
 */
function ScopeClarificationFields({
  requestNumber,
  value,
  onChange,
}: {
  requestNumber: string
  value: ScopeClarificationValue
  onChange: (patch: Partial<ScopeClarificationValue>) => void
}) {
  function updateOption(index: number, patch: Partial<NarrowingOptionState>) {
    onChange({
      narrowingOptions: value.narrowingOptions.map((option, position) =>
        position === index ? { ...option, ...patch } : option,
      ),
    })
  }

  return (
    <>
      <Field label="What this letter is">
        <Select
          value={value.scopeForm}
          onChange={(event) =>
            onChange({
              scopeForm: event.target.value as ScopeClarificationForm,
            })
          }
        >
          <option value="informational">
            Informational — the clock keeps running
          </option>
          <option value="clarification">
            Clarification request — tolls the clock under RCW 42.56.520(3)
          </option>
        </Select>
      </Field>
      <p className="hint">{SCOPE_FORM_CONSEQUENCES[value.scopeForm]}</p>
      <Field label="Items the search matched">
        <Input
          type="number"
          min={0}
          step={1}
          value={value.matchCount}
          onChange={(event) => onChange({ matchCount: event.target.value })}
          required
        />
      </Field>
      <ScopeCountNotice requestNumber={requestNumber} matchCount={value.matchCount} />
      <Field label="What was searched, in plain language">
        <Textarea
          rows={3}
          maxLength={500}
          value={value.querySummary}
          onChange={(event) => onChange({ querySummary: event.target.value })}
          required
        />
      </Field>
      <Field label="Reply requested by">
        <Input
          type="date"
          value={value.responseBy}
          onChange={(event) => onChange({ responseBy: event.target.value })}
          required
        />
      </Field>
      <fieldset className="stack">
        <legend>Narrowing options (optional, up to {MAX_NARROWING_OPTIONS})</legend>
        {value.narrowingOptions.length === 0 && (
          <p className="hint">
            No options offered. A large count with no alternative reads as
            pressure to withdraw rather than as help.
          </p>
        )}
        {value.narrowingOptions.map((option, index) => (
          <div key={option.id} className="stack">
            <Field label={`Option ${index + 1}: search terms (comma separated)`}>
              <Input
                value={option.terms}
                onChange={(event) =>
                  updateOption(index, { terms: event.target.value })
                }
              />
            </Field>
            <Field label={`Option ${index + 1}: what it would cover`}>
              <Textarea
                rows={2}
                maxLength={500}
                value={option.rationale}
                onChange={(event) =>
                  updateOption(index, { rationale: event.target.value })
                }
              />
            </Field>
            <Button
              type="button"
              variant="ghost"
              onClick={() =>
                onChange({
                  narrowingOptions: value.narrowingOptions.filter(
                    (_, position) => position !== index,
                  ),
                })
              }
            >
              Remove option {index + 1}
            </Button>
          </div>
        ))}
        {value.narrowingOptions.length < MAX_NARROWING_OPTIONS && (
          <Button
            type="button"
            variant="secondary"
            onClick={() =>
              onChange({
                narrowingOptions: [
                  ...value.narrowingOptions,
                  newNarrowingOption(),
                ],
              })
            }
          >
            Add a narrowing option
          </Button>
        )}
      </fieldset>
    </>
  )
}

interface InstallmentChoice {
  id: number
  request_number: string
  sequence: number
}

type ConfirmedDeliveryAction = 'approveAndSend' | 'queue' | 'retry'

interface DeliveryActionConfirmation {
  action: ConfirmedDeliveryAction
  item: CorrespondenceDetail
}

const DELIVERY_ACTION_TITLES: Record<ConfirmedDeliveryAction, string> = {
  approveAndSend: 'Send this message to the requester',
  queue: 'Send this approved message to the requester',
  retry: 'Retry correspondence delivery',
}

// A third-party notice is the one outbound letter that never reaches the
// requester; the confirmation must say where it actually goes.
const THIRD_PARTY_ACTION_TITLES: Record<ConfirmedDeliveryAction, string> = {
  approveAndSend: 'Send this notice to the person it names',
  queue: 'Send this approved notice to the person it names',
  retry: DELIVERY_ACTION_TITLES.retry,
}

function deliveryActionTitle({ action, item }: DeliveryActionConfirmation) {
  return item.kind === 'third_party_notice'
    ? THIRD_PARTY_ACTION_TITLES[action]
    : DELIVERY_ACTION_TITLES[action]
}

function deliveryActionRecipient(item: CorrespondenceDetail) {
  return item.kind === 'third_party_notice'
    ? 'This goes to the person it names and any attorney copied, not to the requester.'
    : 'This goes to the requester.'
}

const DELIVERY_ACTION_BUTTONS: Record<ConfirmedDeliveryAction, string> = {
  approveAndSend: 'Send message',
  queue: 'Send message',
  retry: 'Confirm retry',
}

function initialComposeState(requesterEmail: string): ComposeState {
  return {
    kind: 'general',
    subject: '',
    body: '',
    to: requesterEmail,
    cc: '',
    bcc: '',
    estimatedCompletion: '',
    clarificationQuestions: '',
    clarificationDue: '',
    newDate: '',
    reason: '',
    installmentId: '',
    isFinal: false,
    scopeForm: 'informational',
    matchCount: '',
    querySummary: '',
    narrowingOptions: [],
    responseBy: '',
    exemptionCitationIds: [],
    determination: 'no_responsive_records',
    searchCompletedAt: '',
    searchSteps: [emptySearchStep()],
    adequacyReason: '',
    retentionScheduleReference: '',
    incidentHoldChecked: false,
    priorExportsChecked: false,
    emailDriveCopiesChecked: false,
    otherCopiesChecked: false,
    counselApprovalReference: '',
    productionSummary: '',
    estimatedAmount: '',
    estimateBasis: '',
    depositAmount: '',
    amountDue: '',
    paymentRequestedOn: '',
    employeeName: '',
    employeeEmail: '',
    attorneyName: '',
    attorneyEmail: '',
    templateName: '',
  }
}

interface EditState {
  item: CorrespondenceDetail
  subject: string
  body: string
  to: string
  cc: string
  bcc: string
  estimatedCompletion: string
  clarificationQuestions: string
  clarificationDue: string
  newDate: string
  reason: string
  isFinal: boolean
  scopeForm: ScopeClarificationForm
  matchCount: string
  querySummary: string
  narrowingOptions: NarrowingOptionState[]
  responseBy: string
  exemptionCitationIds: number[]
}

function editPayload(state: EditState) {
  const base = {
    expected_version: state.item.version,
    recipients: recipientInputs(state.to, state.cc, state.bcc),
  }
  if (AUTHORED_KINDS.has(state.item.kind as ComposeKind)) {
    return {
      ...base,
      subject: state.subject,
      body: state.body,
      ...(state.item.kind === 'denial'
        ? { exemption_citation_ids: state.exemptionCitationIds }
        : {}),
    }
  }
  if (state.item.kind === 'acknowledgment') {
    return {
      ...base,
      estimated_completion: state.estimatedCompletion,
    }
  }
  if (state.item.kind === 'clarification_request') {
    return {
      ...base,
      clarification_questions: state.clarificationQuestions,
      clarification_due: state.clarificationDue,
    }
  }
  if (state.item.kind === 'scope_clarification') {
    return { ...base, ...scopeClarificationPayload(state) }
  }
  if (state.item.kind === 'estimate_revision') {
    return {
      ...base,
      new_date: state.newDate,
      reason: state.reason,
    }
  }
  return {
    ...base,
    is_final: state.isFinal,
  }
}

/**
 * The four-step statutory tracker, rendered on the screen where the steps are
 * actually performed. The case Overview already carries this guidance, but an
 * officer reading "Human response required" there had no way to act on it
 * without navigating here and hunting for the composer. Nothing about the
 * rule changes: a draft is not a response, an approval is not a response, and
 * only confirmed delivery satisfies RCW 42.56.520 - so each step stays a
 * separate, human-confirmed action.
 */
function StatutoryResponseBanner({
  response,
  canDraft,
  onPrepare,
  onSelectMessage,
}: {
  response: StatutoryResponse
  canDraft: boolean
  onPrepare: () => void
  onSelectMessage: (id: string) => void
}) {
  const step = outstandingStatutoryStep(response)
  const due = response.due_date
  return (
    <div className="mb-4 rounded-lg border border-driftwood/60 bg-skylight/60 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={step ? 'amber' : 'green'}>
          {STATUTORY_RESPONSE_LABELS[response.state] ?? response.state}
        </Badge>
        <span className="text-sm text-pacific/80">
          {due ? `Five-business-day response due ${due}` : 'Due date still calculating'}
        </span>
      </div>
      {response.next_action && (
        <p className="mt-2 font-heading text-sm font-bold text-pacific">
          {`Next: ${response.next_action}`}
        </p>
      )}
      <ol
        aria-label="Statutory response steps"
        className="mt-3 space-y-1.5 text-sm"
      >
        {(response.steps ?? []).map((entry) => (
          <li key={entry.key} className="flex items-start gap-2">
            <span
              aria-hidden="true"
              className={`mt-1.5 size-2 shrink-0 rounded-full ${
                entry.state === 'done'
                  ? 'bg-seaglass'
                  : entry.state === 'current'
                    ? 'bg-whulge'
                    : 'bg-driftwood'
              }`}
            />
            <span
              className={
                entry.state === 'current'
                  ? 'font-heading font-semibold text-pacific'
                  : entry.state === 'done'
                    ? 'text-pacific/80 line-through'
                    : 'text-pacific/80'
              }
            >
              {entry.label}
              <span className="sr-only">
                {entry.state === 'done'
                  ? ' (complete)'
                  : entry.state === 'current'
                    ? ' (do this next)'
                    : ' (not started)'}
              </span>
            </span>
          </li>
        ))}
      </ol>
      {step && canDraft && (
        <div className="mt-3">
          {step.key === 'prepare' ? (
            <Button onClick={onPrepare}>Prepare acknowledgment</Button>
          ) : response.correspondence_id ? (
            <Button
              onClick={() => onSelectMessage(response.correspondence_id as string)}
            >
              {step.key === 'approve'
                ? 'Review and send the draft'
                : 'Open the approved acknowledgment to send it'}
            </Button>
          ) : null}
        </div>
      )}
    </div>
  )
}

interface CorrectedCorrespondence {
  id: string
  kind: CorrespondenceKind
  status: string
  subject: string
  detail?: string
}

interface RequesterCorrectionResult {
  previous_requester: { name: string; email: string }
  requester: { name: string; email: string }
  refreshed_correspondence: CorrectedCorrespondence[]
  correspondence_needing_review: CorrectedCorrespondence[]
  cancelled_correspondence: CorrectedCorrespondence[]
  uncancelled_correspondence: CorrectedCorrespondence[]
}

function CorrectionOutcomeList({
  heading,
  items,
}: {
  heading: string
  items: CorrectedCorrespondence[]
}) {
  if (items.length === 0) return null
  return (
    <div>
      <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
        {heading}
      </p>
      <ul className="mt-1 space-y-1 text-sm text-pacific">
        {items.map((item) => (
          <li key={item.id}>
            {`${CORRESPONDENCE_KIND_LABELS[item.kind] ?? item.kind}: `}
            {item.subject || 'Untitled message'}
            {item.detail ? ` — ${item.detail}` : ''}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function CorrespondencePanel({
  requestNumber,
  roles,
  requesterEmail = '',
  requesterName = '',
  statutoryResponse,
}: {
  requestNumber: string
  roles: StaffRole[]
  requesterEmail?: string
  requesterName?: string
  statutoryResponse?: StatutoryResponse
}) {
  const canDraft = roles.includes('records_officer') || roles.includes('liaison')
  const canManage = roles.includes('records_officer')
  const qc = useQueryClient()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const [composeOpen, setComposeOpen] = useState(false)
  const [compose, setCompose] = useState(() => initialComposeState(requesterEmail))
  const [edit, setEdit] = useState<EditState | null>(null)
  const [preview, setPreview] = useState<CorrespondencePreview | null>(null)
  const [actionConfirmation, setActionConfirmation] =
    useState<DeliveryActionConfirmation | null>(null)
  const [cancelTarget, setCancelTarget] = useState<CorrespondenceDetail | null>(null)
  const [cancelReason, setCancelReason] = useState('')
  const [resolveTarget, setResolveTarget] = useState<ClarificationCycle | null>(null)
  const [resolutionNotes, setResolutionNotes] = useState('')
  const [correctionOpen, setCorrectionOpen] = useState(false)
  const [correctionName, setCorrectionName] = useState('')
  const [correctionEmail, setCorrectionEmail] = useState('')
  const [correctionReason, setCorrectionReason] = useState('')
  const [correctionResult, setCorrectionResult] =
    useState<RequesterCorrectionResult | null>(null)

  const cycles = useQuery({
    queryKey: ['clarification-cycles', requestNumber],
    queryFn: () =>
      api<Page<ClarificationCycle> | ClarificationCycle[]>(
        `/api/staff/clarification-cycles/?request=${encodeURIComponent(requestNumber)}`,
      ),
  })
  const installments = useQuery({
    queryKey: ['installments', requestNumber, 'compose-choice'],
    queryFn: () =>
      api<Page<InstallmentChoice> | InstallmentChoice[]>(
        `/api/staff/installments/?request=${encodeURIComponent(requestNumber)}`,
      ),
    enabled: canManage && composeOpen && compose.kind === 'installment_notice',
  })
  const exemptions = useQuery({
    queryKey: ['exemptions'],
    queryFn: () =>
      api<Page<DenialExemptionCitation> | DenialExemptionCitation[]>(
        '/api/staff/exemptions/',
      ),
    enabled:
      canManage
      && (
        (composeOpen && compose.kind === 'denial')
        || edit?.item.kind === 'denial'
      ),
  })

  const installmentChoices = installments.data
    ? unwrap(installments.data).filter(
        (installment) => installment.request_number === requestNumber,
      )
    : []
  const cycleRows = cycles.data ? unwrap(cycles.data) : []
  const activeExemptions = exemptions.data
    ? unwrap(exemptions.data).filter((citation) => citation.active)
    : []
  const editDenialExemptions = [
    ...activeExemptions,
    ...(
      edit?.item.denial_exemption_citations ?? []
    ).filter(
      (selected) => !activeExemptions.some(
        (active) => active.id === selected.id,
      ),
    ),
  ]

  function refresh(item?: CorrespondenceDetail) {
    if (item) {
      qc.setQueryData(
        correspondenceDetailQueryKey(requestNumber, item.id),
        item,
      )
    }
    void invalidateBoundedInfiniteQueries(qc, ['messages', requestNumber])
    void invalidateBoundedInfiniteQueries(qc, ['message-threads', requestNumber])
    qc.invalidateQueries({ queryKey: ['clarification-cycles', requestNumber] })
    qc.invalidateQueries({ queryKey: ['request', requestNumber] })
    qc.invalidateQueries({ queryKey: ['case-workspace-summary', requestNumber] })
    qc.invalidateQueries({ queryKey: ['case-closure-readiness', requestNumber] })
  }

  /**
   * Which activated starting points exist for the kind being composed.
   * General correspondence is the one kind that may carry several, because it
   * covers every situation that is not a statutory letter; offering the list
   * here is what keeps those wordings inside the application instead of in
   * somebody's drafts folder.
   */
  const templateNames = useQuery({
    queryKey: ['message-template-names'],
    queryFn: () => api<{ templates: { kind: string; names: string[] }[] }>(
      '/api/staff/automatic-message-templates/',
    ),
    enabled: canManage && composeOpen,
    staleTime: 60_000,
  })

  const namedStartingPoints = (
    templateNames.data?.templates.find(
      (entry) => entry.kind === compose.kind,
    )?.names ?? []
  ).filter((name) => name !== '')

  const templatePrefill = useMutation({
    mutationFn: ({ kind, name }: { kind: ComposeKind; name: string }) => api<CorrespondencePreview>(
      '/api/staff/correspondence/template-prefill/'
      + `?request_number=${encodeURIComponent(requestNumber)}`
      + `&kind=${encodeURIComponent(kind)}`
      + `&name=${encodeURIComponent(name)}`,
    ),
    onSuccess: (result) =>
      setCompose((state) => ({
        ...state,
        subject: result.subject,
        body: result.body,
      })),
    onError: (error) => toast('error', error.message),
  })

  const createDraft = useMutation({
    mutationFn: ({ kind, payload }: { kind: ComposeKind; payload: Record<string, unknown> }) =>
      api<CorrespondenceDetail>(
        DRAFT_ENDPOINTS[kind].path,
        { method: DRAFT_ENDPOINTS[kind].method, body: payload },
      ),
    onSuccess: (item) => {
      refresh(item)
      setComposeOpen(false)
      setCompose(initialComposeState(requesterEmail))
      toast('success', 'Correspondence draft created.')
    },
  })

  const editDraft = useMutation({
    mutationFn: (state: EditState) =>
      api<CorrespondenceDetail>(`/api/staff/correspondence/${state.item.id}/`, {
        method: 'PATCH',
        body: editPayload(state),
      }),
    onSuccess: (item) => {
      refresh(item)
      setEdit(null)
      toast('success', 'Draft updated.')
    },
  })

  const previewMessage = useMutation({
    mutationFn: (item: CorrespondenceDetail) =>
      api<CorrespondencePreview>(
        `/api/staff/correspondence/${item.id}/preview/`,
        { fetchAllPages: false },
      ),
    onSuccess: setPreview,
  })

  const approveAndSend = useMutation({
    mutationFn: (item: CorrespondenceDetail) =>
      api<CorrespondenceDetail>(
        `/api/staff/correspondence/${item.id}/approve-and-send/`,
        {
          method: 'POST',
          body: { expected_version: item.version },
        },
      ),
    onSuccess: (item) => {
      refresh(item)
      setActionConfirmation(null)
      toast('success', 'Message approved and sent to the requester.')
    },
  })

  const queue = useMutation({
    mutationFn: (item: CorrespondenceDetail) =>
      api<CorrespondenceDetail>(
        `/api/staff/correspondence/${item.id}/queue/`,
        {
          method: 'POST',
          body: { expected_version: item.version },
        },
      ),
    onSuccess: (item) => {
      refresh(item)
      setActionConfirmation(null)
      toast('success', 'Message sent to the requester.')
    },
  })

  const cancel = useMutation({
    mutationFn: ({
      item,
      reason,
    }: {
      item: CorrespondenceDetail
      reason: string
    }) =>
      api<CorrespondenceDetail>(
        `/api/staff/correspondence/${item.id}/cancel/`,
        {
          method: 'POST',
          body: { expected_version: item.version, reason },
        },
      ),
    onSuccess: (item) => {
      refresh(item)
      setCancelTarget(null)
      setCancelReason('')
      toast('success', 'Unsent correspondence cancelled.')
    },
  })

  const retry = useMutation({
    mutationFn: (item: CorrespondenceDetail) =>
      api<CorrespondenceDetail>(
        `/api/staff/correspondence/${item.id}/retry/`,
        {
          method: 'POST',
          body: { expected_delivery_version: item.delivery?.version },
        },
      ),
    onSuccess: (item) => {
      refresh(item)
      setActionConfirmation(null)
      toast('success', 'Delivery returned to the queue with the same Message-ID.')
    },
  })

  const download = useMutation({
    mutationFn: (attachmentId: string) =>
      api<{ url: string; expires_in: number }>(
        `/api/staff/correspondence-attachments/${attachmentId}/download/`,
        { method: 'POST' },
      ),
    onSuccess: ({ url }) => window.location.assign(url),
  })

  const resolve = useMutation({
    mutationFn: ({
      cycle,
      notes,
    }: {
      cycle: ClarificationCycle
      notes: string
    }) =>
      api<ClarificationCycle>(
        `/api/staff/clarification-cycles/${cycle.id}/resolve/`,
        {
          method: 'POST',
          body: { resolution_notes: notes },
        },
      ),
    onSuccess: () => {
      refresh()
      setResolveTarget(null)
      setResolutionNotes('')
      toast('success', 'Clarification resolved and request returned to gathering.')
    },
  })

  const correctRequester = useMutation({
    mutationFn: (payload: { name: string; email: string; reason: string }) =>
      api<RequesterCorrectionResult>(
        `/api/staff/requests/${encodeURIComponent(requestNumber)}/correct-requester/`,
        { method: 'POST', body: payload },
      ),
    onSuccess: (result) => {
      refresh()
      // A message the correction cancelled or rewrote may be open in the
      // detail pane; its cached record must not keep offering to send it.
      for (const item of [
        ...result.refreshed_correspondence,
        ...result.correspondence_needing_review,
        ...result.cancelled_correspondence,
        ...result.uncancelled_correspondence,
      ]) {
        qc.invalidateQueries({
          queryKey: correspondenceDetailQueryKey(requestNumber, item.id),
        })
      }
      setCorrectionResult(result)
      setCorrectionReason('')
      toast(
        'success',
        `Requester corrected to ${result.requester.email}.`,
      )
    },
  })

  const loadCycle = useMutation({
    mutationFn: (cycleId: string) =>
      api<ClarificationCycle>(
        `/api/staff/clarification-cycles/${cycleId}/`,
      ),
    onSuccess: (cycle) => {
      setResolutionNotes('')
      resolve.reset()
      setResolveTarget(cycle)
    },
  })

  const busy =
    createDraft.isPending
    || editDraft.isPending
    || previewMessage.isPending
    || approveAndSend.isPending
    || queue.isPending
    || cancel.isPending
    || retry.isPending
    || download.isPending
    || loadCycle.isPending
    || resolve.isPending
    || correctRequester.isPending

  const mutationError =
    createDraft.error
    || editDraft.error
    || previewMessage.error
    || cancel.error
    || download.error
    || loadCycle.error
    || resolve.error

  const actionMutation = actionConfirmation?.action === 'approveAndSend'
    ? approveAndSend
    : actionConfirmation?.action === 'queue'
      ? queue
      : actionConfirmation?.action === 'retry'
        ? retry
        : null
  const actionPending = actionMutation?.isPending ?? false
  const actionError = actionMutation?.error

  function composePayload() {
    const base = { request_number: requestNumber }
    if (compose.kind === 'no_records_closure') {
      return {
        ...base,
        determination: compose.determination,
        search_completed_at: compose.searchCompletedAt,
        search_steps: compose.searchSteps.map((step) => ({
          location: step.location,
          custodian: step.custodian,
          method: step.method,
          date_range: step.dateRange,
          result: step.result,
        })),
        adequacy_reason: compose.adequacyReason,
        retention_schedule_reference: compose.retentionScheduleReference,
        incident_hold_checked: compose.incidentHoldChecked,
        prior_exports_checked: compose.priorExportsChecked,
        email_drive_copies_checked: compose.emailDriveCopiesChecked,
        other_copies_checked: compose.otherCopiesChecked,
        counsel_approval_reference: compose.counselApprovalReference,
      }
    }
    if (compose.kind === 'fee_estimate') {
      return {
        ...base,
        estimated_amount: compose.estimatedAmount,
        estimate_basis: compose.estimateBasis,
        // An estimate with no deposit sends no deposit line at all rather
        // than a zero-dollar one.
        ...(compose.depositAmount
          ? { deposit_amount: compose.depositAmount }
          : {}),
      }
    }
    if (compose.kind === 'completion_closure') {
      return { ...base, production_summary: compose.productionSummary }
    }
    if (compose.kind === 'nonpayment_closure') {
      return {
        ...base,
        amount_due: compose.amountDue,
        payment_requested_on: compose.paymentRequestedOn,
      }
    }
    if (compose.kind === 'third_party_notice') {
      return {
        ...base,
        employee_name: compose.employeeName,
        employee_email: compose.employeeEmail,
        attorney_name: compose.attorneyName,
        attorney_email: compose.attorneyEmail,
      }
    }
    if (AUTHORED_KINDS.has(compose.kind)) {
      const recipients = recipientInputs(compose.to, compose.cc, compose.bcc)
      return {
        ...base,
        subject: compose.subject,
        body: compose.body,
        ...(recipients.length > 0 ? { recipients } : {}),
        ...(compose.kind === 'denial'
          ? { exemption_citation_ids: compose.exemptionCitationIds }
          : {}),
      }
    }
    if (compose.kind === 'submission_receipt') return base
    if (compose.kind === 'acknowledgment') {
      return {
        ...base,
        ...(compose.estimatedCompletion
          ? { estimated_completion: compose.estimatedCompletion }
          : {}),
      }
    }
    if (compose.kind === 'clarification_request') {
      return {
        ...base,
        clarification_questions: compose.clarificationQuestions,
        clarification_due: compose.clarificationDue,
      }
    }
    if (compose.kind === 'scope_clarification') {
      return { ...base, ...scopeClarificationPayload(compose) }
    }
    if (compose.kind === 'estimate_revision') {
      return {
        ...base,
        new_date: compose.newDate,
        reason: compose.reason,
      }
    }
    return {
      ...base,
      installment_id: Number(compose.installmentId),
      is_final: compose.isFinal,
    }
  }

  function submitCompose(event: FormEvent) {
    event.preventDefault()
    createDraft.mutate({ kind: compose.kind, payload: composePayload() })
  }

  function openCompose(kind: ComposeKind = 'general') {
    setCompose({ ...initialComposeState(requesterEmail), kind })
    createDraft.reset()
    setComposeOpen(true)
  }

  /**
   * The Inbox and the case header link here with `?compose=<kind>` when the
   * next thing to do is write a particular message. Opening the composer on
   * arrival removes the four blind clicks an officer otherwise spends finding
   * it. The parameter is consumed immediately so a reload or a Back does not
   * reopen a dialog the officer already dismissed.
   */
  const requestedComposeKind = searchParams.get('compose') ?? ''
  const requestedComposeCount = searchParams.get('count') ?? ''
  const resetCreateDraft = createDraft.reset
  useEffect(() => {
    if (!requestedComposeKind) return
    const next = new URLSearchParams(searchParams)
    next.delete('compose')
    next.delete('count')
    setSearchParams(next, { replace: true })
    if (!canDraft) return
    const option = COMPOSE_OPTIONS.find(
      (candidate) =>
        candidate.value === requestedComposeKind
        && (!candidate.officerOnly || canManage),
    )
    if (!option) return
    // The Collection tab links here with the count it is already showing, so
    // the officer does not retype the one number this whole letter is about.
    const matchCount = /^\d+$/.test(requestedComposeCount)
      ? requestedComposeCount
      : ''
    setCompose({
      ...initialComposeState(requesterEmail),
      kind: option.value,
      matchCount,
    })
    resetCreateDraft()
    setComposeOpen(true)
  }, [
    requestedComposeKind,
    requestedComposeCount,
    searchParams,
    setSearchParams,
    canDraft,
    canManage,
    requesterEmail,
    resetCreateDraft,
  ])

  function openEdit(item: CorrespondenceDetail) {
    const metadata = item.draft_metadata
    setEdit({
      item,
      subject: correspondenceSubject(item),
      body: correspondenceBody(item),
      to: addressesFor(item.recipients, 'to'),
      cc: addressesFor(item.recipients, 'cc'),
      bcc: addressesFor(item.recipients, 'bcc'),
      estimatedCompletion: String(metadata.estimated_completion ?? ''),
      clarificationQuestions: String(metadata.clarification_questions ?? ''),
      clarificationDue: String(metadata.clarification_due ?? ''),
      newDate: String(metadata.new_date ?? ''),
      reason: String(metadata.reason ?? ''),
      isFinal: metadata.is_final === true,
      scopeForm: metadata.form === 'clarification'
        ? 'clarification'
        : 'informational',
      matchCount:
        metadata.match_count === undefined || metadata.match_count === null
          ? ''
          : String(metadata.match_count),
      querySummary: String(metadata.query_summary ?? ''),
      narrowingOptions: narrowingOptionsFromMetadata(
        metadata.narrowing_options,
      ),
      responseBy: String(metadata.response_by ?? ''),
      exemptionCitationIds: (
        item.denial_exemption_citations ?? []
      ).map((citation) => citation.id),
    })
    editDraft.reset()
  }

  function openActionConfirmation(
    action: ConfirmedDeliveryAction,
    item: CorrespondenceDetail,
  ) {
    if (action === 'approveAndSend') approveAndSend.reset()
    if (action === 'queue') queue.reset()
    if (action === 'retry') retry.reset()
    setActionConfirmation({ action, item })
  }

  function confirmDeliveryAction() {
    if (!actionConfirmation) return
    if (actionConfirmation.action === 'approveAndSend') {
      approveAndSend.mutate(actionConfirmation.item)
    } else if (actionConfirmation.action === 'queue') {
      queue.mutate(actionConfirmation.item)
    } else {
      retry.mutate(actionConfirmation.item)
    }
  }

  function cycleFor(item: CorrespondenceDetail) {
    return cycleRows.find(
      (cycle) =>
        cycle.outbound_correspondence_id === item.id
        || cycle.inbound_response_correspondence_id === item.id,
    )
  }

  return (
    // No title: the Messages case tab already renders the section heading, and
    // repeating it put two identical headings on the screen.
    <Card
      className="lg:col-span-2"
      actions={
        canDraft
          ? <Button onClick={() => openCompose()}>Compose message</Button>
          : undefined
      }
    >
      <p className="mb-4 text-sm text-pacific/80">
        Django owns the exact conversation record. Request state changes only after
        confirmed delivery, never when a message is merely queued.
      </p>

      {statutoryResponse && (
        <StatutoryResponseBanner
          response={statutoryResponse}
          canDraft={canDraft}
          onPrepare={() => openCompose('acknowledgment')}
          onSelectMessage={(id) => {
            const next = new URLSearchParams(searchParams)
            next.set('message', id)
            setSearchParams(next)
          }}
        />
      )}

      {canManage && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-driftwood/60 bg-white/60 p-3">
          <p className="text-sm text-pacific/80">
            <span className="font-heading font-bold text-pacific">Requester on file:</span>{' '}
            {requesterName || 'Not recorded'}
            {requesterEmail ? ` <${requesterEmail}>` : ''}
          </p>
          <Button
            variant="ghost"
            onClick={() => {
              setCorrectionName(requesterName)
              setCorrectionEmail(requesterEmail)
              setCorrectionReason('')
              setCorrectionResult(null)
              correctRequester.reset()
              setCorrectionOpen(true)
            }}
          >
            Correct requester
          </Button>
        </div>
      )}

      {!canManage && canDraft && (
        <div className="mb-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950">
          <span className="font-heading font-bold">Human approval required.</span>{' '}
          Agents may prepare drafts, but a Records Officer must send
          them in an authenticated staff session.
        </div>
      )}

      {cycles.isError && <ErrorNote error={cycles.error} />}
      {exemptions.isError && <ErrorNote error={exemptions.error} />}
      {mutationError && <ErrorNote error={mutationError} />}

      <MessageWorkspace
        requestNumber={requestNumber}
        roles={roles}
        renderDetail={(item) => (
            <CorrespondenceItem
              item={item}
              cycle={cycleFor(item)}
              canDraft={canDraft}
              canManage={canManage}
              onEdit={openEdit}
              onPreview={(target) => previewMessage.mutate(target)}
              onApprove={(target) => openActionConfirmation('approveAndSend', target)}
              onQueue={(target) => openActionConfirmation('queue', target)}
              onCancel={(target) => {
                setCancelReason('')
                cancel.reset()
                setCancelTarget(target)
              }}
              onRetry={(target) => openActionConfirmation('retry', target)}
              onDownload={(attachmentId) => download.mutate(attachmentId)}
              busy={busy}
            />
        )}
      />

      {cycleRows.length > 0 && (
        <div className="mt-5 border-t border-driftwood/50 pt-4">
          <h3 className="text-sm text-pacific">Clarification review</h3>
          <div className="mt-2 space-y-2">
            {cycleRows.map((cycle) => (
              <div
                key={cycle.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-driftwood/40 bg-skylight/50 px-3 py-2"
              >
                <div>
                  <p className="font-heading text-sm font-bold text-pacific">
                    {cycleState(cycle)}
                  </p>
                  <p className="text-xs text-pacific/80">
                    Opened {cycle.opened_at.slice(0, 10)}
                    {cycle.responded_at ? ` · response ${cycle.responded_at.slice(0, 10)}` : ''}
                  </p>
                </div>
                {canManage
                  && cycle.responded_at
                  && !cycle.resolved_at
                  && !cycle.cancelled_at && (
                    <Button
                      onClick={() => loadCycle.mutate(cycle.id)}
                    >
                      Resolve clarification
                    </Button>
                  )}
              </div>
            ))}
          </div>
        </div>
      )}

      <Modal
        open={composeOpen && canDraft}
        title="Compose correspondence"
        onClose={() => setComposeOpen(false)}
      >
        <form onSubmit={submitCompose} className="space-y-4">
          <Field label="Message type">
            <Select
              value={compose.kind}
              onChange={(event) =>
                setCompose(() => ({
                  ...initialComposeState(requesterEmail),
                  kind: event.target.value as ComposeKind,
                }))
              }
              className="w-full"
            >
              {COMPOSE_OPTIONS.filter(
                (option) => !option.officerOnly || canManage,
              ).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>

          {AUTHORED_KINDS.has(compose.kind) && (
            <>
              <div className="flex flex-wrap items-center gap-3 rounded-md border border-driftwood/60 bg-skylight/50 px-3 py-2">
                <p className="text-sm text-pacific/80">
                  This message is authored by you. The district template is a
                  starting point you can edit before saving.
                </p>
                {namedStartingPoints.length > 0 && (
                  <Select
                    aria-label="District template"
                    value={compose.templateName}
                    onChange={(event) =>
                      setCompose((state) => ({
                        ...state,
                        templateName: event.target.value,
                      }))
                    }
                  >
                    <option value="">District default</option>
                    {namedStartingPoints.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </Select>
                )}
                <Button
                  type="button"
                  variant="ghost"
                  disabled={templatePrefill.isPending}
                  onClick={() =>
                    templatePrefill.mutate({
                      kind: compose.kind,
                      name: compose.templateName,
                    })
                  }
                >
                  {templatePrefill.isPending
                    ? 'Loading template…'
                    : 'Start from district template'}
                </Button>
              </div>
              <Field label="Subject">
                <Input
                  value={compose.subject}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, subject: event.target.value }))
                  }
                  required
                />
              </Field>
              <Field label="Message">
                <Textarea
                  rows={7}
                  value={compose.body}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, body: event.target.value }))
                  }
                  required
                />
              </Field>
              <Field
                label="To"
                hint="Comma-separated. Leave blank to use the requester on file."
              >
                <Input
                  type="text"
                  value={compose.to}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, to: event.target.value }))
                  }
                />
              </Field>
              <Field label="Cc">
                <Input
                  type="text"
                  value={compose.cc}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, cc: event.target.value }))
                  }
                />
              </Field>
              <Field label="Bcc">
                <Input
                  type="text"
                  value={compose.bcc}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, bcc: event.target.value }))
                  }
                />
              </Field>
            </>
          )}

          {compose.kind === 'denial' && (
            <>
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950">
                <span className="font-heading font-bold">
                  Full-exemption denial only.
                </span>{' '}
                No-records and out-of-retention determinations require a
                separate adequate-search workflow; do not substitute an
                exemption citation.
              </div>
              <fieldset className="space-y-1">
                <legend className="font-heading text-sm font-bold text-pacific">
                  Exemption citations
                </legend>
                <p className="text-xs text-pacific/80">
                  Required structured legal basis. Citations marked for second
                  review cannot yet be approved.
                </p>
                {exemptions.isPending ? (
                  <Spinner label="Loading exemption citations…" />
                ) : activeExemptions.length === 0 ? (
                  <p className="text-sm text-red-800">
                    No active, approval-eligible exemption citations are available.
                  </p>
                ) : (
                  <div className="space-y-2 rounded-md border border-driftwood/40 p-3">
                    {activeExemptions.map((citation) => (
                      <label
                        key={citation.id}
                        className="flex items-start gap-2 text-sm text-pacific"
                      >
                        <input
                          type="checkbox"
                          checked={compose.exemptionCitationIds.includes(citation.id)}
                          disabled={
                            citation.requires_second_review
                            && !compose.exemptionCitationIds.includes(citation.id)
                          }
                          onChange={(event) =>
                            setCompose((state) => ({
                              ...state,
                              exemptionCitationIds: toggleCitation(
                                state.exemptionCitationIds,
                                citation.id,
                                event.target.checked,
                              ),
                            }))
                          }
                        />
                        <span>
                          <span className="font-heading font-bold">
                            {citation.citation}
                          </span>
                          {' — '}
                          {citation.short_name}
                          {citation.requires_second_review
                            ? ' (second-review evidence not yet supported)'
                            : ''}
                        </span>
                      </label>
                    ))}
                  </div>
                )}
              </fieldset>
            </>
          )}

          {compose.kind === 'no_records_closure' && (
            <>
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950">
                <span className="font-heading font-bold">
                  Human legal determination.
                </span>{' '}
                Attest only after an objectively reasonable search, completion
                of every collection task, review of obvious leads, and district
                counsel approval. The evidence is retained and cannot be edited.
              </div>
              <Field label="Determination">
                <Select
                  value={compose.determination}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      determination: event.target.value as ComposeState['determination'],
                    }))
                  }
                  required
                >
                  <option value="no_responsive_records">
                    No responsive records located
                  </option>
                  <option value="outside_retention">
                    Requested records outside retention
                  </option>
                  <option value="not_maintained">
                    No record maintained; the District is not required to create one
                  </option>
                </Select>
              </Field>
              <Field label="Search completed at">
                <Input
                  type="datetime-local"
                  value={compose.searchCompletedAt}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      searchCompletedAt: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              <fieldset className="space-y-3">
                <legend className="font-heading text-sm font-bold text-pacific">
                  Search record
                </legend>
                <p className="text-xs text-pacific/80">
                  Identify each likely location, custodian, method, date range,
                  and result. Follow and document obvious leads.
                </p>
                {compose.searchSteps.map((step, index) => (
                  <div
                    key={index}
                    className="space-y-3 rounded-md border border-driftwood/40 p-3"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-heading text-sm font-bold text-pacific">
                        Search step {index + 1}
                      </p>
                      {compose.searchSteps.length > 1 && (
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() =>
                            setCompose((state) => ({
                              ...state,
                              searchSteps: state.searchSteps.filter(
                                (_, stepIndex) => stepIndex !== index,
                              ),
                            }))
                          }
                        >
                          Remove step
                        </Button>
                      )}
                    </div>
                    {([
                      ['location', 'Location searched'],
                      ['custodian', 'Custodian'],
                      ['method', 'Search method and terms'],
                      ['dateRange', 'Date range'],
                      ['result', 'Result and leads followed'],
                    ] as const).map(([field, label]) => (
                      <Field key={field} label={label}>
                        <Input
                          value={step[field]}
                          onChange={(event) =>
                            setCompose((state) => ({
                              ...state,
                              searchSteps: state.searchSteps.map(
                                (candidate, stepIndex) =>
                                  stepIndex === index
                                    ? { ...candidate, [field]: event.target.value }
                                    : candidate,
                              ),
                            }))
                          }
                          required
                        />
                      </Field>
                    ))}
                  </div>
                ))}
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() =>
                    setCompose((state) => ({
                      ...state,
                      searchSteps: [...state.searchSteps, emptySearchStep()],
                    }))
                  }
                >
                  Add search step
                </Button>
              </fieldset>
              <Field
                label="Why this search was adequate"
                hint="Explain why the searched locations were reasonably likely to contain responsive records."
              >
                <Textarea
                  rows={5}
                  minLength={20}
                  value={compose.adequacyReason}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      adequacyReason: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              {compose.determination === 'outside_retention' && (
                <>
                  <Field label="Verified retention schedule reference">
                    <Textarea
                      rows={3}
                      minLength={10}
                      value={compose.retentionScheduleReference}
                      onChange={(event) =>
                        setCompose((state) => ({
                          ...state,
                          retentionScheduleReference: event.target.value,
                        }))
                      }
                      required
                    />
                  </Field>
                  <fieldset className="space-y-2">
                    <legend className="font-heading text-sm font-bold text-pacific">
                      Obvious-lead review
                    </legend>
                    {([
                      ['incidentHoldChecked', 'Incident or litigation holds checked'],
                      ['priorExportsChecked', 'Prior exports and preservation copies checked'],
                      ['emailDriveCopiesChecked', 'Email and Drive copies checked'],
                      ['otherCopiesChecked', 'Other likely copies and locations checked'],
                    ] as const).map(([field, label]) => (
                      <label
                        key={field}
                        className="flex items-start gap-2 text-sm text-pacific"
                      >
                        <input
                          type="checkbox"
                          checked={compose[field]}
                          onChange={(event) =>
                            setCompose((state) => ({
                              ...state,
                              [field]: event.target.checked,
                            }))
                          }
                          required
                        />
                        <span>{label}</span>
                      </label>
                    ))}
                  </fieldset>
                </>
              )}
              <Field
                label="District counsel approval reference"
                hint="Enter the retained memo, ticket, or other substantive approval reference."
              >
                <Input
                  minLength={10}
                  value={compose.counselApprovalReference}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      counselApprovalReference: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
            </>
          )}

          {compose.kind === 'acknowledgment' && (
            <Field
              label="Estimated completion date"
              hint="Leave blank to use the district's standard estimate. Set a date only to override it for this request."
            >
              <Input
                type="date"
                value={compose.estimatedCompletion}
                onChange={(event) =>
                  setCompose((state) => ({
                    ...state,
                    estimatedCompletion: event.target.value,
                  }))
                }
              />
            </Field>
          )}

          {compose.kind === 'clarification_request' && (
            <>
              <Field label="Clarification questions">
                <Textarea
                  rows={5}
                  value={compose.clarificationQuestions}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      clarificationQuestions: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              <Field label="Response requested by">
                <Input
                  type="date"
                  value={compose.clarificationDue}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      clarificationDue: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
            </>
          )}

          {compose.kind === 'fee_estimate' && (
            <>
              <Field
                label="Estimated copying charge"
                hint="Calculated from the adopted fee schedule. RCW 42.56.120 allows copying costs only, never search or review time."
              >
                <Input
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={compose.estimatedAmount}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      estimatedAmount: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              <Field
                label="How the charge was calculated"
                hint="Appears in the letter so the requester can check it against the adopted schedule."
              >
                <Textarea
                  rows={4}
                  value={compose.estimateBasis}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      estimateBasis: event.target.value,
                    }))
                  }
                  minLength={20}
                  required
                />
              </Field>
              <Field
                label="Deposit requested (optional)"
                hint="At most ten percent of the estimate (RCW 42.56.120(2)). Leave blank to send an estimate with no deposit. Sending this letter does not hold work by itself — apply a deposit gate on Releases to do that."
              >
                <Input
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={compose.depositAmount}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      depositAmount: event.target.value,
                    }))
                  }
                />
              </Field>
            </>
          )}

          {compose.kind === 'completion_closure' && (
            <Field
              label="What the District produced"
              hint="Appears in the letter and becomes the closure note on the case. Delivery closes the request as fulfilled."
            >
              <Textarea
                rows={4}
                value={compose.productionSummary}
                onChange={(event) =>
                  setCompose((state) => ({
                    ...state,
                    productionSummary: event.target.value,
                  }))
                }
                minLength={20}
                required
              />
            </Field>
          )}

          {compose.kind === 'nonpayment_closure' && (
            <>
              <Field
                label="Amount requested"
                hint="The fee, deposit, or installment charge that went unpaid."
              >
                <Input
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={compose.amountDue}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      amountDue: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              <Field
                label="Payment requested on"
                hint="Delivery closes the request as abandoned with the reason code fee_unpaid."
              >
                <Input
                  type="date"
                  value={compose.paymentRequestedOn}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      paymentRequestedOn: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
            </>
          )}

          {compose.kind === 'third_party_notice' && (
            <>
              <Field
                label="Person notified"
                hint="This notice goes to them, not to the requester, and gives them ten business days to seek a court order."
              >
                <Input
                  value={compose.employeeName}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      employeeName: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              <Field label="Their email address">
                <Input
                  type="email"
                  value={compose.employeeEmail}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      employeeEmail: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              <Field label="Their attorney (optional)">
                <Input
                  value={compose.attorneyName}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      attorneyName: event.target.value,
                    }))
                  }
                />
              </Field>
              <Field
                label="Attorney email (optional)"
                hint="Copied on the notice. Required if an attorney is named."
              >
                <Input
                  type="email"
                  value={compose.attorneyEmail}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      attorneyEmail: event.target.value,
                    }))
                  }
                />
              </Field>
            </>
          )}

          {compose.kind === 'scope_clarification' && (
            <ScopeClarificationFields
              requestNumber={requestNumber}
              value={compose}
              onChange={(patch) =>
                setCompose((state) => ({ ...state, ...patch }))
              }
            />
          )}

          {compose.kind === 'estimate_revision' && (
            <>
              <Field label="New estimated completion date">
                <Input
                  type="date"
                  value={compose.newDate}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, newDate: event.target.value }))
                  }
                  required
                />
              </Field>
              <Field
                label="Substantive reason"
                hint="Required. The accountable Records Officer reviews the reason before approval."
              >
                <Textarea
                  rows={4}
                  value={compose.reason}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, reason: event.target.value }))
                  }
                  required
                />
              </Field>
            </>
          )}

          {compose.kind === 'installment_notice' && (
            <>
              <Field label="Installment">
                <Select
                  value={compose.installmentId}
                  onChange={(event) =>
                    setCompose((state) => ({
                      ...state,
                      installmentId: event.target.value,
                    }))
                  }
                  disabled={installments.isPending || installments.isError}
                  required
                >
                  <option value="">Select an installment</option>
                  {installmentChoices.map((installment) => (
                    <option key={installment.id} value={installment.id}>
                      {installment.request_number} — Installment {installment.sequence}
                    </option>
                  ))}
                </Select>
              </Field>
              {installments.isPending && <Spinner label="Loading installments…" />}
              {installments.isError && <ErrorNote error={installments.error} />}
              <p className="text-sm text-pacific/80">
                The secure download link is built by the server from the
                installment you chose. There is nothing to type, and nothing to
                mistype.
              </p>
              <label className="flex items-center gap-2 text-sm text-pacific">
                <input
                  type="checkbox"
                  checked={compose.isFinal}
                  onChange={(event) =>
                    setCompose((state) => ({ ...state, isFinal: event.target.checked }))
                  }
                />
                This is the final installment
              </label>
            </>
          )}

          {createDraft.isError && <ErrorNote error={createDraft.error} />}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setComposeOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                createDraft.isPending
                || (
                  compose.kind === 'denial'
                  && compose.exemptionCitationIds.length === 0
                )
              }
            >
              {createDraft.isPending ? 'Creating…' : 'Create draft'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={edit !== null}
        title="Edit correspondence draft"
        onClose={() => setEdit(null)}
      >
        {edit && (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              editDraft.mutate(edit)
            }}
          >
            {AUTHORED_KINDS.has(edit.item.kind as ComposeKind) && (
              <>
                <Field label="Subject">
                  <Input
                    value={edit.subject}
                    onChange={(event) =>
                      setEdit((state) =>
                        state ? { ...state, subject: event.target.value } : state,
                      )
                    }
                    required
                  />
                </Field>
                <Field label="Message">
                  <Textarea
                    rows={8}
                    value={edit.body}
                    onChange={(event) =>
                      setEdit((state) =>
                        state ? { ...state, body: event.target.value } : state,
                      )
                    }
                    required
                  />
                </Field>
              </>
            )}
            {edit.item.kind === 'acknowledgment' && (
              <Field label="Estimated completion date">
                <Input
                  type="date"
                  value={edit.estimatedCompletion}
                  onChange={(event) =>
                    setEdit((state) =>
                      state
                        ? { ...state, estimatedCompletion: event.target.value }
                        : state,
                    )
                  }
                  required
                />
              </Field>
            )}
            {edit.item.kind === 'clarification_request' && (
              <>
                <Field label="Clarification questions">
                  <Textarea
                    rows={5}
                    value={edit.clarificationQuestions}
                    onChange={(event) =>
                      setEdit((state) =>
                        state
                          ? {
                              ...state,
                              clarificationQuestions: event.target.value,
                            }
                          : state,
                      )
                    }
                    required
                  />
                </Field>
                <Field label="Response requested by">
                  <Input
                    type="date"
                    value={edit.clarificationDue}
                    onChange={(event) =>
                      setEdit((state) =>
                        state
                          ? { ...state, clarificationDue: event.target.value }
                          : state,
                      )
                    }
                    required
                  />
                </Field>
              </>
            )}
            {edit.item.kind === 'scope_clarification' && (
              <ScopeClarificationFields
                requestNumber={requestNumber}
                value={edit}
                onChange={(patch) =>
                  setEdit((state) => (state ? { ...state, ...patch } : state))
                }
              />
            )}
            {edit.item.kind === 'estimate_revision' && (
              <>
                <Field label="New estimated completion date">
                  <Input
                    type="date"
                    value={edit.newDate}
                    onChange={(event) =>
                      setEdit((state) =>
                        state ? { ...state, newDate: event.target.value } : state,
                      )
                    }
                    required
                  />
                </Field>
                <Field label="Substantive reason">
                  <Textarea
                    rows={4}
                    value={edit.reason}
                    onChange={(event) =>
                      setEdit((state) =>
                        state ? { ...state, reason: event.target.value } : state,
                      )
                    }
                    required
                  />
                </Field>
              </>
            )}
            {edit.item.kind === 'installment_notice' && (
              <>
                <p className="text-sm text-pacific/80">
                  The secure download link comes from the installment itself
                  and is re-derived on every edit.
                </p>
                <label className="flex items-center gap-2 text-sm text-pacific">
                  <input
                    type="checkbox"
                    checked={edit.isFinal}
                    onChange={(event) =>
                      setEdit((state) =>
                        state ? { ...state, isFinal: event.target.checked } : state,
                      )
                    }
                  />
                  This is the final installment
                </label>
              </>
            )}
            {edit.item.kind === 'denial' && (
              <fieldset className="space-y-1">
                <legend className="font-heading text-sm font-bold text-pacific">
                  Exemption citations
                </legend>
                <p className="text-xs text-pacific/80">
                  Changing this list replaces the draft legal-basis snapshot.
                </p>
                {exemptions.isPending ? (
                  <Spinner label="Loading exemption citations…" />
                ) : (
                  <div className="space-y-2 rounded-md border border-driftwood/40 p-3">
                    {editDenialExemptions.map((citation) => (
                      <label
                        key={citation.id}
                        className="flex items-start gap-2 text-sm text-pacific"
                      >
                        <input
                          type="checkbox"
                          checked={edit.exemptionCitationIds.includes(citation.id)}
                          disabled={
                            (citation.requires_second_review || !citation.active)
                            && !edit.exemptionCitationIds.includes(citation.id)
                          }
                          onChange={(event) =>
                            setEdit((state) =>
                              state
                                ? {
                                    ...state,
                                    exemptionCitationIds: toggleCitation(
                                      state.exemptionCitationIds,
                                      citation.id,
                                      event.target.checked,
                                    ),
                                  }
                                : state,
                            )
                          }
                        />
                        <span>
                          <span className="font-heading font-bold">
                            {citation.citation}
                          </span>
                          {' — '}
                          {citation.short_name}
                          {!citation.active ? ' (inactive; remove before saving)' : ''}
                          {citation.requires_second_review
                            ? ' (second-review evidence not yet supported)'
                            : ''}
                        </span>
                      </label>
                    ))}
                  </div>
                )}
              </fieldset>
            )}
            <Field label="To">
              <Input
                value={edit.to}
                onChange={(event) =>
                  setEdit((state) =>
                    state ? { ...state, to: event.target.value } : state,
                  )
                }
                required
              />
            </Field>
            <Field label="Cc">
              <Input
                value={edit.cc}
                onChange={(event) =>
                  setEdit((state) =>
                    state ? { ...state, cc: event.target.value } : state,
                  )
                }
              />
            </Field>
            <Field label="Bcc">
              <Input
                value={edit.bcc}
                onChange={(event) =>
                  setEdit((state) =>
                    state ? { ...state, bcc: event.target.value } : state,
                  )
                }
              />
            </Field>
            {editDraft.isError && <ErrorNote error={editDraft.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setEdit(null)}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  editDraft.isPending
                  || (
                    edit.item.kind === 'denial'
                    && edit.exemptionCitationIds.length === 0
                  )
                }
              >
                {editDraft.isPending ? 'Saving…' : 'Save draft'}
              </Button>
            </div>
          </form>
        )}
      </Modal>

      <Modal
        open={preview !== null}
        title="Exact delivery preview"
        onClose={() => setPreview(null)}
      >
        {preview && (
          <div className="space-y-4">
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                Subject
              </p>
              <p className="mt-1 text-sm text-pacific">{preview.subject}</p>
            </div>
            {(preview.denial_exemption_citations ?? []).length > 0 && (
              <div>
                <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                  Denial exemption basis
                </p>
                <ul className="mt-1 list-disc pl-5 text-sm text-pacific">
                  {preview.denial_exemption_citations.map((citation) => (
                    <li key={citation.id}>
                      {citation.citation} — {citation.short_name}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                Recipients
              </p>
              <p className="mt-1 break-all text-sm text-pacific">
                {preview.recipients
                  .map(
                    (recipient) => {
                      const address = recipient.display_name.trim()
                        ? `${recipient.display_name} <${recipient.email}>`
                        : recipient.email
                      return `${recipientTypeLabel(recipient.recipient_type)}: ${address}`
                    },
                  )
                  .join(' · ')}
              </p>
            </div>
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                Attachments ({preview.attachments.length})
              </p>
              {preview.attachments.length === 0 ? (
                <p className="mt-1 text-sm text-pacific/80">No attachments.</p>
              ) : (
                <ul className="mt-1 space-y-2">
                  {preview.attachments.map((attachment) => (
                    <li
                      key={attachment.id}
                      className="rounded-md border border-driftwood/40 bg-white p-3"
                    >
                      <p className="break-all text-sm font-medium text-pacific">
                        {attachment.original_filename}
                      </p>
                      <p className="mt-1 text-xs text-pacific/80">
                        {humanFileSize(attachment.size)}
                        {' · '}
                        {attachment.content_type}
                      </p>
                      <p className="mt-1 text-xs text-pacific/80">
                        {scanStatusLabel(attachment.scan_status)}
                        {' · '}
                        {attachment.quarantined
                          ? 'Held in quarantine'
                          : 'Released from quarantine'}
                        {' · '}
                        {attachment.is_downloadable
                          ? 'Available to download'
                          : 'Not available to download'}
                      </p>
                      {/* The checksum is integrity evidence, not something an
                          officer reads. Keep it available, out of the way. */}
                      <details className="mt-1">
                        <summary className="cursor-pointer text-xs text-pacific/80">
                          File checksum
                        </summary>
                        <p className="mt-1 break-all font-mono text-xs text-pacific/80">
                          SHA-256 {attachment.sha256}
                        </p>
                      </details>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                Message
              </p>
              <div className="mt-1 whitespace-pre-wrap rounded-md border border-driftwood/40 bg-white p-3 text-sm text-pacific">
                {preview.body}
              </div>
            </div>
            <div className="flex justify-end">
              <Button variant="ghost" onClick={() => setPreview(null)}>
                Close preview
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={actionConfirmation !== null && canManage}
        dismissible={!actionPending}
        title={
          actionConfirmation
            ? deliveryActionTitle(actionConfirmation)
            : 'Confirm correspondence action'
        }
        onClose={() => {
          if (!actionPending) setActionConfirmation(null)
        }}
      >
        {actionConfirmation && (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              confirmDeliveryAction()
            }}
          >
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                Subject
              </p>
              <p className="mt-1 text-sm text-pacific">
                {correspondenceSubject(actionConfirmation.item)}
              </p>
            </div>
            <p className="text-sm text-pacific/80">
              {actionConfirmation.action === 'approveAndSend'
                ? `${deliveryActionRecipient(actionConfirmation.item)} Confirm you have reviewed the exact message, recipients, attachments, and legal basis — sending freezes this content as the record of what was sent.`
                : actionConfirmation.action === 'queue'
                  ? `${deliveryActionRecipient(actionConfirmation.item)} It was approved earlier, so the content is already frozen.`
                  : 'Retrying requeues the same approved message and preserves its Message-ID. Confirm that the prior failure is safe to retry.'}
            </p>
            {actionError && <ErrorNote error={actionError} />}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setActionConfirmation(null)}
                disabled={actionPending}
              >
                Go back
              </Button>
              <Button type="submit" disabled={actionPending}>
                {actionPending
                  ? 'Working…'
                  : DELIVERY_ACTION_BUTTONS[actionConfirmation.action]}
              </Button>
            </div>
          </form>
        )}
      </Modal>

      <Modal
        open={cancelTarget !== null && canManage}
        title="Cancel correspondence"
        onClose={() => setCancelTarget(null)}
      >
        {cancelTarget && (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              cancel.mutate({ item: cancelTarget, reason: cancelReason })
            }}
          >
            <p className="text-sm text-pacific/80">
              Cancellation is allowed only before a successful send and is recorded
              in the audit trail.
            </p>
            <Field label="Cancellation reason">
              <Textarea
                rows={4}
                value={cancelReason}
                onChange={(event) => setCancelReason(event.target.value)}
                required
              />
            </Field>
            {cancel.isError && <ErrorNote error={cancel.error} />}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setCancelTarget(null)}
              >
                Keep message
              </Button>
              <Button
                type="submit"
                variant="danger"
                disabled={cancel.isPending || !cancelReason.trim()}
              >
                {cancel.isPending ? 'Cancelling…' : 'Confirm cancellation'}
              </Button>
            </div>
          </form>
        )}
      </Modal>

      <Modal
        open={correctionOpen && canManage}
        title="Correct requester"
        onClose={() => setCorrectionOpen(false)}
      >
        {correctionResult ? (
          <div className="space-y-4">
            <p className="text-sm text-pacific/80">
              {`The record now names ${correctionResult.requester.name} `}
              {`<${correctionResult.requester.email}>, replacing `}
              {`${correctionResult.previous_requester.name} `}
              {`<${correctionResult.previous_requester.email}>. What was `}
              submitted is retained unchanged, and nothing already sent was
              altered.
            </p>
            <CorrectionOutcomeList
              heading="Drafts now addressing the corrected requester"
              items={correctionResult.refreshed_correspondence}
            />
            <CorrectionOutcomeList
              heading="Drafts you must re-read before sending"
              items={correctionResult.correspondence_needing_review}
            />
            <CorrectionOutcomeList
              heading="Approved messages cancelled — draft replacements"
              items={correctionResult.cancelled_correspondence}
            />
            <CorrectionOutcomeList
              heading="Already in delivery — could not be recalled"
              items={correctionResult.uncancelled_correspondence}
            />
            <div className="flex justify-end">
              <Button onClick={() => setCorrectionOpen(false)}>Done</Button>
            </div>
          </div>
        ) : (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              correctRequester.mutate({
                name: correctionName,
                email: correctionEmail,
                reason: correctionReason,
              })
            }}
          >
            <p className="text-sm text-pacific/80">
              Use this when the request was forwarded and intake recorded the
              sender instead of the requester. What was submitted is kept as
              the record of what arrived; open drafts are re-addressed to the
              corrected requester, and approved messages still addressed to the
              old identity are cancelled so they are not delivered to the wrong
              person. No statutory date changes.
            </p>
            <Field label="Requester name">
              <Input
                value={correctionName}
                onChange={(event) => setCorrectionName(event.target.value)}
                required
              />
            </Field>
            <Field label="Requester email">
              <Input
                type="email"
                value={correctionEmail}
                onChange={(event) => setCorrectionEmail(event.target.value)}
                required
              />
            </Field>
            <Field
              label="Reason for the correction"
              hint={reasonHint('name the evidence that this is the requester')}
            >
              <Textarea
                rows={4}
                value={correctionReason}
                onChange={(event) => setCorrectionReason(event.target.value)}
                required
              />
            </Field>
            {reasonShortfall(correctionReason) && (
              <p className="text-sm text-pacific/80">
                {reasonShortfall(correctionReason)}
              </p>
            )}
            {correctRequester.isError && (
              <ErrorNote error={correctRequester.error} />
            )}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setCorrectionOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  correctRequester.isPending
                  || reasonTooShort(correctionReason)
                  || !correctionName.trim()
                  || !correctionEmail.trim()
                }
              >
                {correctRequester.isPending
                  ? 'Correcting…'
                  : 'Correct requester'}
              </Button>
            </div>
          </form>
        )}
      </Modal>

      <Modal
        open={resolveTarget !== null && canManage}
        title="Resolve clarification"
        onClose={() => setResolveTarget(null)}
      >
        {resolveTarget && (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              resolve.mutate({ cycle: resolveTarget, notes: resolutionNotes })
            }}
          >
            <p className="text-sm text-pacific/80">
              Receiving a reply does not resolve the legal clarification state.
              Confirm the scope and record substantive resolution notes.
            </p>
            <Field label="Resolution notes">
              <Textarea
                rows={5}
                value={resolutionNotes}
                onChange={(event) => setResolutionNotes(event.target.value)}
                required
              />
            </Field>
            {resolve.isError && <ErrorNote error={resolve.error} />}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setResolveTarget(null)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={resolve.isPending || !resolutionNotes.trim()}
              >
                {resolve.isPending ? 'Resolving…' : 'Resolve cycle'}
              </Button>
            </div>
          </form>
        )}
      </Modal>
    </Card>
  )
}
