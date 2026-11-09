// ABOUTME: The intake labels a classification may suggest, and how staff
// ABOUTME: screens name them.
export type IntakeLabel =
  | 'new_request'
  | 'follow_up'
  | 'duplicate_of'
  | 'related_to'
  | 'non_request'
  | 'spam'

export const INTAKE_LABEL_TEXT: Record<IntakeLabel, string> = {
  new_request: 'New request',
  follow_up: 'Follow-up to',
  duplicate_of: 'Duplicate of',
  related_to: 'Related to',
  non_request: 'Not a records request',
  spam: 'Spam',
}

export type IntakeAction = 'add_to_case' | 'merge' | 'close_non_request'

/**
 * The ordinary staff action a confirmed label opens, or null when there is
 * none: a new or related request needs no action, and a web-form case has no
 * non-request closure.
 */
export function intakeAction(
  label: IntakeLabel,
  target: string | null,
  emailAutoCreated: boolean,
): IntakeAction | null {
  if (label === 'follow_up' && target && emailAutoCreated) return 'add_to_case'
  if ((label === 'follow_up' || label === 'duplicate_of') && target) return 'merge'
  if ((label === 'non_request' || label === 'spam') && emailAutoCreated) return 'close_non_request'
  return null
}
