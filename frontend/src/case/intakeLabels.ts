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
