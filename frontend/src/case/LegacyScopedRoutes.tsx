// ABOUTME: Compatibility redirects for /requests/:number and /correspondence.
// ABOUTME: Both serve live routes; the resource-scoped work redirects are gone.
import { Navigate, useParams, useSearchParams } from 'react-router'
import type { StaffRole } from '../api/auth'

const REQUEST_NUMBER = /^[A-Za-z0-9-]{1,20}$/
const POSITIVE_INTEGER = /^[1-9][0-9]*$/
const MAX_DATABASE_ID = '9223372036854775807'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const DATE = /^\d{4}-\d{2}-\d{2}$/
const safeToken = (value: string) => value.length <= 150 && /^[A-Za-z0-9@._+-]+$/.test(value)
const oneOf = (...values: string[]) => (value: string) => values.includes(value)
const uuid = (value: string) => UUID.test(value)
const databaseId = (value: string) => (
  POSITIVE_INTEGER.test(value)
  && (
    value.length < MAX_DATABASE_ID.length
    || (value.length === MAX_DATABASE_ID.length && value <= MAX_DATABASE_ID)
  )
)
const isoDate = (value: string) => {
  if (!DATE.test(value)) return false
  const [year = NaN, month = NaN, day = NaN] = value.split('-').map(Number)
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= (days[month - 1] ?? 0)
}

function withQuery(path: string, parameters: URLSearchParams) {
  const query = parameters.toString()
  return query ? `${path}?${query}` : path
}

const MESSAGE_FILTERS = [
  'direction',
  'kind',
  'status',
  'work_state',
  'read_state',
  'delivery_state',
  'matching_state',
  'has_attachments',
  'date_from',
  'date_to',
  'assignee',
] as const

const MESSAGE_FILTER_VALIDATORS: Record<typeof MESSAGE_FILTERS[number], (value: string) => boolean> = {
  direction: oneOf('inbound', 'outbound'),
  kind: oneOf(
    'acknowledgment',
    'clarification_request',
    'clarification_response',
    'delivery_status_notification',
    'estimate_revision',
    'installment_notice',
    'responsive_records_notice',
    'submission_receipt',
    'closure',
    'no_records_closure',
    'denial',
    'general',
  ),
  status: oneOf('draft', 'approved', 'queued', 'sent', 'failed', 'cancelled', 'received', 'unmatched', 'quarantined'),
  work_state: oneOf('needs_attention', 'waiting', 'handled', 'no_action_required', 'source_excluded'),
  read_state: oneOf('read', 'unread'),
  delivery_state: oneOf('pending', 'claimed', 'awaiting_reconciliation', 'sent', 'failed', 'retryable', 'cancelled'),
  matching_state: oneOf('matched', 'candidate', 'unmatched', 'ambiguous', 'duplicate', 'quarantined'),
  has_attachments: oneOf('true', 'false'),
  date_from: isoDate,
  date_to: isoDate,
  assignee: databaseId,
}

function requestMessagesDestination(source: URLSearchParams, requestNumber: string) {
  const destination = new URLSearchParams()
  const selectedMessage = source.get('message')?.trim() ?? ''
  if (uuid(selectedMessage)) destination.set('message', selectedMessage)
  if (source.get('message_mode') === 'threads') destination.set('message_mode', 'threads')
  const selectedThread = source.get('message_thread')?.trim() ?? ''
  if (safeToken(selectedThread)) destination.set('message_thread', selectedThread)
  if (source.get('message_sort') === 'oldest') destination.set('message_sort', 'oldest')
  const legacyStatus = source.get('status')?.trim() ?? ''
  const explicitKind = (
    source.get('message_kind')?.trim() || source.get('kind')?.trim() || ''
  )
  if (legacyStatus === 'delivery_status_notification' && !explicitKind) {
    destination.set('message_kind', 'delivery_status_notification')
  }
  for (const filter of MESSAGE_FILTERS) {
    const destinationKey = `message_${filter}`
    const value = [source.get(destinationKey), source.get(filter)]
      .map((candidate) => candidate?.trim() ?? '')
      .find((candidate) => candidate && MESSAGE_FILTER_VALIDATORS[filter](candidate))
    if (value) {
      destination.set(destinationKey, value)
    }
  }
  const dateFrom = destination.get('message_date_from')
  const dateTo = destination.get('message_date_to')
  if (dateFrom && dateTo && dateFrom > dateTo) {
    destination.delete('message_date_from')
    destination.delete('message_date_to')
  }
  return withQuery(
    `/requests/${encodeURIComponent(requestNumber)}/messages`,
    destination,
  )
}

export function LegacyRequestRedirect() {
  const { number = '' } = useParams<{ number: string }>()
  const [source] = useSearchParams()
  if (!REQUEST_NUMBER.test(number)) return <Navigate to="/cases" replace />
  const message = source.get('message')?.trim() ?? ''
  if (uuid(message)) {
    return <Navigate to={requestMessagesDestination(source, number)} replace />
  }
  return <Navigate to={`/requests/${encodeURIComponent(number)}/intake`} replace />
}

function legacyInboundView(source: URLSearchParams) {
  const status = source.get('status')?.trim() ?? ''
  const matchingState = source.get('matching_state')?.trim() ?? ''
  if (status === 'quarantined' || matchingState === 'quarantined') {
    return 'quarantined'
  }
  if (
    status === 'unmatched'
    || ['unmatched', 'candidate', 'ambiguous'].includes(matchingState)
  ) {
    return 'unmatched'
  }
  return null
}

export function LegacyCorrespondenceRedirect({ roles }: { roles: StaffRole[] }) {
  const [source] = useSearchParams()
  const requestNumber = source.get('request')?.trim() ?? ''
  const canUseMessages = roles.includes('records_officer') || roles.includes('liaison')
  const receipt = source.get('inbound_receipt')?.trim() ?? ''
  const status = source.get('status')?.trim() ?? ''
  const inboundView = legacyInboundView(source)
  if (receipt && uuid(receipt) && roles.includes('records_officer')) {
    const destination = new URLSearchParams({
      view: inboundView ?? 'unmatched',
      inbound_receipt: receipt,
    })
    return <Navigate to={withQuery('/inbox', destination)} replace />
  }
  if (REQUEST_NUMBER.test(requestNumber) && canUseMessages) {
    return <Navigate to={requestMessagesDestination(source, requestNumber)} replace />
  }
  if (
    (roles.includes('records_officer') || roles.includes('admin'))
    && (
      status === 'failed'
      || status === 'queued'
      || status === 'draft'
      || status === 'delivery_status_notification'
    )
  ) {
    return (
      <Navigate
        to={`/configuration/correspondence?focus=${status}`}
        replace
      />
    )
  }
  if (roles.includes('records_officer') && status === 'received') {
    return <Navigate to="/inbox?view=received" replace />
  }
  if (roles.includes('records_officer') && inboundView) {
    return <Navigate to={`/inbox?view=${inboundView}`} replace />
  }
  if (roles.includes('admin') && !roles.includes('records_officer')) {
    return <Navigate to="/configuration/correspondence" replace />
  }
  if (!roles.includes('records_officer')) return <Navigate to="/cases" replace />

  const destination = new URLSearchParams()
  if (receipt && uuid(receipt)) {
    destination.set('view', 'unmatched')
    destination.set('inbound_receipt', receipt)
  }
  return <Navigate to={withQuery('/inbox', destination)} replace />
}
