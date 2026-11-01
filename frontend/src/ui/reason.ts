// ABOUTME: One definition of the substantive-reason rule the API enforces.
// ABOUTME: Screens must show reasonHint() wherever reasonTooShort() gates a button.

/**
 * Several actions require a written reason that lands in the audit trail. The
 * server rejects anything shorter, so the UI must say so up front: a disabled
 * button with no explanation reads as a broken dialog, not as a rule.
 */
export const MIN_REASON_LENGTH = 10

export function reasonTooShort(value: string): boolean {
  return value.trim().length < MIN_REASON_LENGTH
}

/** Field hint. Pass `detail` to say what the reason should actually cover. */
export function reasonHint(detail?: string): string {
  const rule = `Required — at least ${MIN_REASON_LENGTH} characters`
  return detail ? `${rule}; ${detail}` : rule
}

/**
 * Live feedback under a reason field: how many more characters are needed.
 * Returns an empty string once the reason is long enough.
 */
export function reasonShortfall(value: string): string {
  const remaining = MIN_REASON_LENGTH - value.trim().length
  if (remaining <= 0) return ''
  return `${remaining} more character${remaining === 1 ? '' : 's'} needed`
}
