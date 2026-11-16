// ABOUTME: One wording and tone for footage-preservation and camera-hold states.
// ABOUTME: Shared by the Preservations screen and the case page's footage card.

export type PreservationTone = 'green' | 'amber' | 'red' | 'neutral'

export const PRESERVATION_STATE_TONES: Record<string, PreservationTone> = {
  pending: 'amber',
  held: 'green',
  partial: 'amber',
  manual: 'amber',
  no_cameras: 'amber',
  expired: 'red',
  released: 'neutral',
  excluded: 'neutral',
  error: 'red',
  failed: 'red',
  gap: 'amber',
  not_retained: 'amber',
  release_failed: 'red',
}

// Every state that leaves footage unaccounted for says a human must act:
// nothing here is resolved by waiting.
const ADEQUATE_SEARCH = 'adequate-search review required'

export const PRESERVATION_STATE_LABELS: Record<string, string> = {
  pending: 'Pending — hold executing',
  held: 'Held',
  partial: 'Partly held — gaps or failures need review',
  manual: 'Manual hold required',
  no_cameras: `No cameras in Aware at the requested site — ${ADEQUATE_SEARCH}`,
  expired: `Window outside retention — ${ADEQUATE_SEARCH}`,
  released: 'Released',
  // Camera-level states
  gap: `Footage gap — ${ADEQUATE_SEARCH}`,
  not_retained: `No retained footage — ${ADEQUATE_SEARCH}`,
  failed: 'Failed',
  release_failed: 'Not yet confirmed deleted — bookmark may still exist; the release retries automatically',
}

export function preservationStateLabel(state: string): string {
  return PRESERVATION_STATE_LABELS[state] ?? state
}

export function preservationStateTone(state: string): PreservationTone {
  return PRESERVATION_STATE_TONES[state] ?? 'neutral'
}
