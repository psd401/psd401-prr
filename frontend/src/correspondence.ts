// ABOUTME: Staff correspondence API types and display vocabulary.
// ABOUTME: Shared by request conversation and global delivery-review screens.

export type CorrespondenceDirection = 'outbound' | 'inbound'

export function correspondenceDetailQueryKey(requestNumber: string, id: string) {
  return ['correspondence-detail', requestNumber, id] as const
}

export type CorrespondenceKind =
  | 'acknowledgment'
  | 'clarification_request'
  | 'clarification_response'
  | 'delivery_status_notification'
  | 'estimate_revision'
  | 'interpretation_estimate'
  | 'fee_estimate'
  | 'installment_notice'
  | 'submission_receipt'
  | 'responsive_records_notice'
  | 'scope_clarification'
  | 'closure'
  | 'no_records_closure'
  | 'completion_closure'
  | 'nonpayment_closure'
  | 'third_party_notice'
  | 'denial'
  | 'general'

export interface MessageWorkItem {
  id: string
  work_kind: string
  assignee_id: number | null
  assignee_username: string | null
  disposition:
    | 'needs_attention'
    | 'waiting'
    | 'handled'
    | 'no_action_required'
    | 'source_excluded'
  version: number
  is_read: boolean
  read_at: string | null
  disposed_at?: string | null
  disposed_by_username?: string | null
  no_action_category?: string
  no_action_reason?: string
  no_action_by_username?: string | null
  no_action_at?: string | null
  events?: Array<{
    id: string
    event_type: string
    actor_username: string | null
    occurred_at: string
    resulting_version: number
    metadata: Record<string, unknown>
  }>
}

/**
 * Bounded case state carried on a cross-case row so an officer can triage it
 * without opening every case. Case metadata only — never requester identity.
 */
export interface MessageCaseContext {
  legal_status: string
  statutory_response_state: string
  statutory_response_due: string | null
  email_intake_auto_created: boolean
}

export interface CorrespondenceSummary {
  id: string
  request_number: string
  direction: CorrespondenceDirection
  kind: CorrespondenceKind
  status: string
  subject: string
  snippet: string
  automated: boolean
  created_at: string
  approved_at: string | null
  queued_at: string | null
  sent_at: string | null
  received_at: string | null
  matched_at: string | null
  failed_at: string | null
  cancelled_at: string | null
  failure_category: string
  version: number
  recipient_count: number
  attachment_count: number
  delivery_status: string | null
  matching_status: string | null
  resulting_status_event_id: number | null
  inbound_receipt_id: string | null
  work_item: MessageWorkItem | null
  case_context: MessageCaseContext | null
}

export interface CorrespondenceThreadSummary {
  id: string
  evidence_id: string
  message_count: number
  latest_at: string
  subject: string
}

export interface CorrespondenceRecipient {
  id: number
  recipient_type: 'to' | 'cc' | 'bcc'
  email: string
  display_name: string
  requester_relationship: boolean
  created_at: string
}

export interface CorrespondenceAttachment {
  id: string
  original_filename: string
  content_type: string
  size: number
  sha256: string
  scan_status: 'pending' | 'clean' | 'infected' | 'error'
  quarantined: boolean
  quarantine_reason: string
  is_downloadable: boolean
  created_at: string
  scanned_at: string | null
}

export interface DenialExemptionCitation {
  id: number
  citation: string
  short_name: string
  description: string
  active: boolean
  requires_second_review: boolean
}

export interface CorrespondenceDelivery {
  id: string
  status: string
  provider: string
  provider_account: string
  claim_expires_at: string | null
  attempt_count: number
  successful_attempt_number: number | null
  last_failure_category: string
  last_failure_description: string
  provider_message_id: string
  provider_thread_id: string
  queued_at: string
  completed_at: string | null
  version: number
}

export interface CorrespondenceDetail extends CorrespondenceSummary {
  draft_subject: string
  draft_body: string
  draft_metadata: Record<string, unknown>
  approved_subject: string
  approved_body: string
  approved_metadata: Record<string, unknown>
  received_subject: string
  received_body: string
  received_sender: string
  received_recipients: string[]
  template_identifier: string
  template_version: string
  created_by: string | null
  approved_by: string | null
  queued_by: string | null
  matched_by: string | null
  rfc_message_id: string
  provider: string
  provider_account: string
  provider_message_id: string
  provider_thread_id: string
  failure_description: string
  replaces_id: string | null
  resulting_status: string | null
  recipients: CorrespondenceRecipient[]
  attachments: CorrespondenceAttachment[]
  denial_exemption_citations: DenialExemptionCitation[]
  delivery: CorrespondenceDelivery | null
}

export interface CorrespondencePreview {
  correspondence_id: string
  status: string
  version: number
  subject: string
  body: string
  recipients: CorrespondenceRecipient[]
  attachments: CorrespondenceAttachment[]
  denial_exemption_citations: DenialExemptionCitation[]
}

export interface DeliveryAttempt {
  id: number
  attempt_number: number
  claim_owner: string
  claimed_at: string
  result_at: string | null
  result_category: string
  provider_message_id: string
  provider_thread_id: string
  error_category: string
  actor: string | null
  created_at: string
}

export interface ClarificationCycle {
  id: string
  request_number: string
  outbound_correspondence_id: string
  inbound_response_correspondence_id: string | null
  opened_at: string
  responded_at: string | null
  resolved_at: string | null
  cancelled_at: string | null
  resolved_by?: string | null
  resolution_notes?: string
  opened_status_event_id?: number | null
  resolved_status_event_id?: number | null
}

export interface InboundReceiptSummary {
  id: string
  correspondence_id: string | null
  parsing_status: string
  matching_status: string
  match_method: string
  candidate_request_number: string | null
  matched_request_number: string | null
  quarantined: boolean
  quarantine_reason_code: string
  parsing_error_code: string
  received_at: string
  created_at: string
  completed_at: string | null
  attachment_count: number
  downloadable_attachment_count: number
  quarantined_attachment_count: number
  version: number
  /** Who wrote and what about; null for a message the parser could not read. */
  received_sender: string | null
  received_subject: string | null
  /** The open work item behind the row, for a no-action decision. */
  work_item_id: string | null
  work_item_version: number | null
}

export interface InboundReceiptAttachment {
  id: string
  original_filename: string
  content_type: string
  size: number
  scan_status: 'pending' | 'clean' | 'infected' | 'error'
  quarantined: boolean
  quarantine_reason_code: string
  is_downloadable: boolean
  created_at: string
  scanned_at: string | null
}

export interface DeliveryStatusNotification {
  id: string
  delivery_id: string
  recipient_index: number
  recipient_match_status: 'exact' | 'unmatched' | 'ambiguous'
  action: 'failed' | 'delayed' | 'delivered' | 'relayed' | 'expanded'
  enhanced_status: string
  created_at: string
}

export interface InboundReceiptDetail extends InboundReceiptSummary {
  provider: string
  provider_account: string
  provider_thread_id: string
  rfc_message_id: string
  delivery_status_original_message_id: string
  matched_delivery_id: string | null
  in_reply_to: string
  references: string[]
  envelope_sender: string
  envelope_recipients: string[]
  received_subject: string | null
  received_body: string | null
  received_sender: string | null
  received_recipients: string[] | null
  received_headers: Record<string, string>
  correspondence_status: string | null
  correspondence_kind: string | null
  attachments: InboundReceiptAttachment[]
  delivery_notifications: DeliveryStatusNotification[]
}

export interface CorrespondenceOperations {
  generated_at: string
  counts: {
    queued: number
    claimed: number
    awaiting_reconciliation: number
    retryable: number
    failed: number
    sent: number
    cancelled: number
    stuck: number
    inbound: number
    unmatched: number
    quarantined_messages: number
    quarantined_attachments: number
    delivery_exceptions: number
  }
  ages_seconds: {
    oldest_delivery: number | null
    oldest_claim: number | null
    oldest_stuck: number | null
  }
  failure_categories: Record<string, number>
}

export interface CorrespondenceReadiness {
  ready: boolean
  checks: {
    component:
      | 'outbound_transport'
      | 'object_storage'
      | 'malware_scanner'
      | 'task_broker'
      | 'gmail_transport'
    status: 'ready' | 'not_ready'
    code:
      | 'enabled'
      | 'disabled'
      | 'available'
      | 'unavailable'
      | 'configured'
  }[]
}

export const CORRESPONDENCE_KIND_LABELS: Record<CorrespondenceKind, string> = {
  acknowledgment: 'Acknowledgment',
  clarification_request: 'Clarification request',
  clarification_response: 'Clarification response',
  delivery_status_notification: 'Delivery status report',
  estimate_revision: 'Estimate revision',
  interpretation_estimate: 'Interpretation and estimate',
  fee_estimate: 'Fee estimate',
  installment_notice: 'Installment notice',
  submission_receipt: 'Submission receipt',
  responsive_records_notice: 'Responsive records',
  scope_clarification: 'Scope conversation',
  closure: 'Closure',
  no_records_closure: 'No-records / out-of-retention closure',
  completion_closure: 'Completion closure',
  nonpayment_closure: 'Non-payment closure',
  third_party_notice: 'Third-party notice',
  denial: 'Denial',
  general: 'General correspondence',
}

export const CORRESPONDENCE_STATUS_LABELS: Record<string, string> = {
  draft: 'Draft',
  approved: 'Approved',
  queued: 'Queued',
  sent: 'Sent',
  failed: 'Failed',
  cancelled: 'Cancelled',
  received: 'Received',
  unmatched: 'Unmatched',
  quarantined: 'Quarantined',
}

export const DELIVERY_STATUS_LABELS: Record<string, string> = {
  pending: 'Pending delivery',
  claimed: 'Claimed',
  awaiting_reconciliation: 'Awaiting reconciliation',
  sent: 'Sent',
  failed: 'Failed',
  retryable: 'Retryable',
  cancelled: 'Cancelled',
}

export const REQUEST_STATUS_EFFECT_LABELS: Record<string, string> = {
  received: 'Received',
  acknowledged: 'Acknowledged',
  clarification: 'Awaiting clarification',
  gathering: 'Gathering records',
  review: 'Review and redaction',
  installment: 'Producing installments',
  fulfilled: 'Fulfilled',
  closed_withdrawn: 'Closed — withdrawn',
  closed_abandoned: 'Closed — abandoned',
  closed_non_request: 'Closed — not a records request',
  closed_no_records: 'Closed — no responsive records',
  closed_duplicate: 'Closed — duplicate, merged into another case',
  denied: 'Denied',
}

/** Malware-scan states, in words a records officer can act on. */
export const SCAN_STATUS_LABELS: Record<string, string> = {
  pending: 'Scanning for malware',
  clean: 'Scanned — clean',
  infected: 'Blocked — malware detected',
  error: 'Scan unavailable',
}

export function scanStatusLabel(status: string): string {
  return SCAN_STATUS_LABELS[status] ?? `Unknown scan state (${status})`
}

/** Envelope roles. "TO"/"CC" shouted in caps is not a label. */
export const RECIPIENT_TYPE_LABELS: Record<string, string> = {
  to: 'To',
  cc: 'Cc',
  bcc: 'Bcc',
  reply_to: 'Reply-To',
}

export function recipientTypeLabel(recipientType: string): string {
  return RECIPIENT_TYPE_LABELS[recipientType] ?? recipientType
}

export type BadgeTone = 'neutral' | 'green' | 'blue' | 'amber' | 'red'

export function correspondenceStatusTone(status: string): BadgeTone {
  if (status === 'sent' || status === 'received') return 'green'
  if (status === 'queued' || status === 'approved') return 'blue'
  if (status === 'draft' || status === 'unmatched') return 'amber'
  if (status === 'failed' || status === 'quarantined') return 'red'
  return 'neutral'
}

export function correspondenceSubject(item: CorrespondenceDetail): string {
  if (item.direction === 'inbound') return item.received_subject
  return item.approved_subject || item.draft_subject
}

export function correspondenceBody(item: CorrespondenceDetail): string {
  if (item.direction === 'inbound') return item.received_body
  return item.approved_body || item.draft_body
}

export function correspondenceTimestamp(item: CorrespondenceSummary): string {
  return (
    item.sent_at
    || item.received_at
    || item.failed_at
    || item.queued_at
    || item.approved_at
    || item.created_at
  )
}

export function humanFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
