// ABOUTME: The one place a RequestStatus becomes words and a colour.
// ABOUTME: Four screens used to keep private copies and one of them drifted.

/**
 * Legal status labels, mirroring `RequestStatus` in `apps/records/models.py`.
 *
 * This module exists because the same map was previously duplicated in
 * `pages/RequestDetail.tsx`, `case/CaseWorkspace.tsx`, `pages/Requests.tsx`
 * and `correspondence.ts`. The labels agreed; the *tones* did not, and the
 * case workspace rendered every closed case in the same blue it used for an
 * active one. A single exported pair is the only way that stays fixed.
 */
export const STATUS_LABELS: Record<string, string> = {
  received: 'Received',
  acknowledged: 'Acknowledged',
  clarification: 'Awaiting Clarification',
  gathering: 'Gathering Records',
  review: 'Review & Redaction',
  installment: 'Producing Installments',
  fulfilled: 'Fulfilled',
  closed_withdrawn: 'Closed – Withdrawn',
  closed_abandoned: 'Closed – Abandoned',
  closed_non_request: 'Closed – Not a Records Request',
  closed_no_records: 'Closed – No Responsive Records',
  closed_duplicate: 'Closed – Duplicate',
  denied: 'Denied (fully exempt)',
}

export type StatusTone = 'blue' | 'amber' | 'neutral' | 'red'

/**
 * Blue means the case is live and owes work. Neutral means it is finished.
 * Amber means it is waiting on the requester. Red is reserved for a denial,
 * which is the one outcome an officer may need to defend in court.
 */
export const STATUS_TONES: Record<string, StatusTone> = {
  received: 'blue',
  acknowledged: 'blue',
  clarification: 'amber',
  gathering: 'blue',
  review: 'blue',
  installment: 'blue',
  fulfilled: 'neutral',
  closed_withdrawn: 'neutral',
  closed_abandoned: 'neutral',
  closed_non_request: 'neutral',
  closed_no_records: 'neutral',
  closed_duplicate: 'neutral',
  denied: 'red',
}

/** The statuses that mean no further work is owed on the case. */
export const TERMINAL_STATUSES = new Set([
  'fulfilled',
  'closed_withdrawn',
  'closed_abandoned',
  'closed_non_request',
  'closed_no_records',
  'closed_duplicate',
  'denied',
])

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

/** Unknown statuses fall back to neutral rather than borrowing "active" blue. */
export function statusTone(status: string): StatusTone {
  return STATUS_TONES[status] ?? 'neutral'
}

export function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATUSES.has(status)
}

/** The status list a case filter offers, derived from the one label map. */
export const CASE_STATUS_CHOICES = Object.entries(STATUS_LABELS).map(
  ([value, label]) => ({ value, label }),
)
