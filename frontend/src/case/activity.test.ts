import { describe, expect, it } from 'vitest'
import { boundedActivity, type ActivityEntry } from './activity'

function entry(
  id: string,
  occurred_at: string,
  flagged = false,
): ActivityEntry {
  return {
    id,
    type: 'lifecycle',
    occurred_at,
    label: id,
    actor_username: null,
    detail: null,
    work_item_id: null,
    delivery_status: null,
    flagged,
  }
}

describe('boundedActivity', () => {
  it('fills by recency when nothing is flagged', () => {
    const entries = [
      entry('e5', '2026-08-05T00:00:00Z'),
      entry('e4', '2026-08-04T00:00:00Z'),
      entry('e3', '2026-08-03T00:00:00Z'),
      entry('e2', '2026-08-02T00:00:00Z'),
    ]
    expect(boundedActivity(entries, 2).map((e) => e.id)).toEqual(['e5', 'e4'])
  })

  it('keeps a backdated compliance note that recency would have cut', () => {
    // The importer backdates "no initial response was recorded" to the day the
    // request arrived. On a case with any routine activity since, a plain
    // newest-first slice drops the one entry that changes what staff must do.
    const entries = [
      entry('recent-3', '2026-08-05T00:00:00Z'),
      entry('recent-2', '2026-08-04T00:00:00Z'),
      entry('recent-1', '2026-08-03T00:00:00Z'),
      entry('migration-gap', '2024-01-09T00:00:00Z', true),
    ]
    const shown = boundedActivity(entries, 3).map((e) => e.id)
    expect(shown).toContain('migration-gap')
    expect(shown).toHaveLength(3)
  })

  it('still reads as a timeline after seating a flagged entry', () => {
    const entries = [
      entry('newest', '2026-08-05T00:00:00Z'),
      entry('older', '2026-08-04T00:00:00Z'),
      entry('ancient-flag', '2024-01-09T00:00:00Z', true),
    ]
    expect(boundedActivity(entries, 3).map((e) => e.id)).toEqual([
      'newest',
      'older',
      'ancient-flag',
    ])
  })

  it('never returns more than the limit even when everything is flagged', () => {
    const entries = [
      entry('f1', '2026-08-05T00:00:00Z', true),
      entry('f2', '2026-08-04T00:00:00Z', true),
      entry('f3', '2026-08-03T00:00:00Z', true),
    ]
    expect(boundedActivity(entries, 2)).toHaveLength(2)
  })

  it('returns everything when the limit exceeds what exists', () => {
    const entries = [entry('only', '2026-08-05T00:00:00Z')]
    expect(boundedActivity(entries, 5)).toHaveLength(1)
  })
})
