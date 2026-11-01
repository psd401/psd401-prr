// ABOUTME: What a bounded case timeline is allowed to leave out.
// ABOUTME: Compliance notes are backdated, so recency alone would bury them.

export interface ActivityEntry {
  id: string
  type: 'lifecycle' | 'work' | 'delivery'
  occurred_at: string
  label: string
  actor_username: string | null
  detail: string | null
  work_item_id: string | null
  delivery_status: string | null
  /**
   * A migration note that changes what an officer must do about this case --
   * "the source system recorded no initial response", "these files were not
   * migrated". The importer backdates them to the request's receipt date, so
   * a plain newest-first cut buries them the moment anything routine happens.
   */
  flagged: boolean
}

/**
 * Choose the entries a bounded excerpt shows.
 *
 * Recency alone is the wrong rule: the entries that most change an officer's
 * next move are the oldest ones on a migrated case. Flagged entries take
 * their slots first, the rest fill by recency, and the result is put back in
 * reverse-chronological order so it still reads as a timeline.
 */
export function boundedActivity(
  entries: ActivityEntry[],
  limit: number,
): ActivityEntry[] {
  const flagged = entries.filter((entry) => entry.flagged).slice(0, limit)
  const seated = new Set(flagged.map((entry) => entry.id))
  const recent = entries
    .filter((entry) => !seated.has(entry.id))
    .slice(0, Math.max(0, limit - flagged.length))
  return [...flagged, ...recent].sort((a, b) =>
    a.occurred_at < b.occurred_at ? 1 : a.occurred_at > b.occurred_at ? -1 : 0,
  )
}
