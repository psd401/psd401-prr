// ABOUTME: One vocabulary for the RCW 42.56.520 initial-response state machine.
// ABOUTME: Case header, Messages banner, and Inbox rows must never disagree.

/** One of the four things that must happen for a five-day response to exist. */
export interface StatutoryResponseStep {
  key: string
  label: string
  state: 'done' | 'current' | 'pending'
}

/** The bounded statutory position of one case, as the staff API reports it. */
export interface StatutoryResponse {
  state: string
  due_date: string | null
  next_action: string
  steps: StatutoryResponseStep[]
  correspondence_id: string | null
}

/** Full state names, for a case header that has room for them. */
export const STATUTORY_RESPONSE_LABELS: Record<string, string> = {
  delivered: 'Initial response delivered',
  scheduled: 'Safety net scheduled',
  due_for_safety_net: 'Initial response due',
  manual_required: 'Human response required',
  automatic_blocked: 'Automatic response held',
  queued: 'Initial response queued',
  attention_required: 'Initial response needs attention',
  terminal_excluded: 'Closed – no response was owed',
  terminal_unverified: 'Closed with no five-day response on record',
  merged: 'Merged – response tracked on the case of record',
  human_draft: 'Response draft exists',
  approved_not_queued: 'Response approved, not queued',
  deadline_calculating: 'Deadline calculating',
}

/**
 * The same states phrased as what is still outstanding, for a list row where
 * an officer is scanning many cases at once. "Five-day response not sent" is
 * the fact that decides whether to open the row; "Safety net scheduled" is
 * not, because a scheduled safety net is not a delivered response.
 */
export const STATUTORY_ROW_LABELS: Record<string, string> = {
  delivered: 'Five-day response delivered',
  scheduled: 'Five-day response not sent',
  due_for_safety_net: 'Five-day response overdue',
  manual_required: 'Five-day response not sent',
  automatic_blocked: 'Automatic acknowledgment held',
  queued: 'Acknowledgment queued for delivery',
  attention_required: 'Acknowledgment delivery failed',
  terminal_excluded: 'Closed – no response owed',
  terminal_unverified: 'Closed – no five-day response on record',
  merged: 'Merged – tracked on the case of record',
  human_draft: 'Acknowledgment drafted, not approved',
  approved_not_queued: 'Acknowledgment approved, not queued',
  deadline_calculating: 'Deadline still calculating',
}

export function statutoryRowLabel(state: string): string {
  return STATUTORY_ROW_LABELS[state] ?? state
}

/** Amber once the case owes a response nobody has delivered yet. */
export function statutoryRowTone(state: string): 'neutral' | 'green' | 'amber' | 'red' {
  if (state === 'delivered') return 'green'
  // Only closed-as-not-a-records-request is settled. Every other terminal
  // state reaching the row means the case was closed with no first response
  // on record, which is a live statutory exposure and must not be greyed out
  // to look calmer than a case that was actually answered.
  if (state === 'terminal_excluded') return 'neutral'
  // A merged duplicate owes nothing itself; its case of record carries the
  // obligation and is where any exposure shows.
  if (state === 'merged') return 'neutral'
  if (state === 'terminal_unverified') return 'red'
  if (state === 'attention_required' || state === 'automatic_blocked') return 'red'
  if (state === 'queued') return 'neutral'
  return 'amber'
}

/**
 * The step an officer must perform next, or null when the deadline is met,
 * the case is terminal, or the only remaining step belongs to the mail
 * provider. A draft, an approval, and a queued message are all steps toward
 * a response; none of them is one.
 */
export function outstandingStatutoryStep(
  response: StatutoryResponse | undefined,
): StatutoryResponseStep | null {
  if (!response) return null
  if (
    response.state === 'delivered' ||
    response.state === 'terminal_excluded' ||
    // Closed with no response on record: the acknowledgment ladder no longer
    // applies, so no step is "next". The badge carries the exposure instead.
    response.state === 'terminal_unverified'
  ) {
    return null
  }
  const current = (response.steps ?? []).find((step) => step.state === 'current')
  if (!current || current.key === 'deliver') return null
  return current
}
