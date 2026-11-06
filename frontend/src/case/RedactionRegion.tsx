// ABOUTME: One redaction box drawn over a page preview: saved (black), the
// ABOUTME: person's draft (amber), or an AI suggestion (blue).
import type { NormalizedBox } from './redactionCanvas'

export function Region({
  box,
  draft = false,
  proposal = false,
}: {
  box: NormalizedBox
  draft?: boolean
  proposal?: boolean
}) {
  return (
    <span
      aria-hidden="true"
      className={`pointer-events-none absolute border-2 ${
        draft
          ? 'border-warning bg-warning/40'
          : proposal
            ? 'border-info bg-info/30'
            : 'border-danger bg-black/70'
      }`}
      style={{
        left: `${box.x0 * 100}%`,
        top: `${box.y0 * 100}%`,
        width: `${(box.x1 - box.x0) * 100}%`,
        height: `${(box.y1 - box.y0) * 100}%`,
      }}
    />
  )
}
