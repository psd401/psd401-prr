// ABOUTME: PII-minimal work-item API contracts and shared queue vocabulary.
// ABOUTME: Legal status and message content remain owned by their source domains.
export interface WorkItemSummary {
  id: string
  source_type: 'request' | 'correspondence' | 'inbound_receipt' | 'collection_task' | 'record_item'
  source_id: string
  work_kind: string
  request_number: string | null
  assignee_id: number | null
  assignee_username: string | null
  disposition: 'needs_attention' | 'waiting' | 'handled' | 'no_action_required' | 'source_excluded'
  version: number
  is_read: boolean
  read_at: string | null
  urgency_date: string | null
  next_action: string
  detail_url: string
  source_url: string | null
  disposed_at: string | null
  created_at: string
  updated_at: string
}

export interface WorkItemEvent {
  id: string
  event_type: string
  actor_id: number | null
  actor_username: string | null
  occurred_at: string
  expected_version: number | null
  resulting_version: number
  metadata: Record<string, unknown>
}

export interface WorkItemDetail extends WorkItemSummary {
  disposed_by_id: number | null
  disposed_by_username: string | null
  no_action_category: string
  no_action_reason: string
  no_action_by_id: number | null
  no_action_by_username: string | null
  no_action_at: string | null
  events: WorkItemEvent[]
}

export interface WorkItemCounts {
  total: number
  unresolved: number
  needs_attention: number
  waiting: number
  handled: number
  no_action_required: number
  source_excluded: number
  personal: number
  team: number
  unassigned: number
  critical: number
  recently_changed: number
  review: number
  inbox_needs_attention: number
  /** Open cases with at least one requester email nobody has handled. */
  inbox_updated_cases?: number
  /** Open cases visible to this user: what the Cases view lists. */
  inbox_open_cases?: number
  inbox_awaiting_statutory_response: number
  inbox_new: number
  inbox_unmatched: number
  inbox_quarantined: number
  inbox_delivery_exceptions: number
  /** Cases with a fee or deposit unanswered for thirty days. */
  inbox_fee_unpaid: number
  inbox_waiting: number
  inbox_handled: number
  inbox_no_action: number
  inbox_all: number
}

export const DISPOSITION_LABELS: Record<WorkItemSummary['disposition'], string> = {
  needs_attention: 'Needs attention',
  waiting: 'Waiting',
  handled: 'Handled',
  no_action_required: 'No action required',
  source_excluded: 'Source excluded',
}

export function itemCountLabel(count: number, label: string) {
  return `${count} ${count === 1 ? 'item' : 'items'} in ${label}`
}
