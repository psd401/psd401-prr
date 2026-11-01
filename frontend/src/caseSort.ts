// ABOUTME: The case table's sort vocabulary: URL text in, table state out.
// ABOUTME: Sorting is server-side, so these keys must match the API whitelist.
import type { TableSort } from './ui'

/**
 * Server ordering keys the case list whitelists, mirroring `CASE_ORDERINGS`
 * in `apps/api/resources/requests.py`. A key this set does not contain is
 * dropped rather than sent: a bookmarked URL from an older build must not put
 * the table into a state the server silently ignores while the header claims
 * the rows are sorted.
 */
export const CASE_SORT_KEYS = new Set([
  'number', 'requester', 'subject', 'status', 'received', 'due', 'estimated',
  'assignee',
])

/** Column headings for each key, for saying in words what order is on screen. */
export const CASE_SORT_LABELS: Record<string, string> = {
  number: 'Case #',
  requester: 'Requester',
  subject: 'Subject',
  status: 'Status',
  received: 'Received',
  due: 'Next due',
  estimated: 'Estimated',
  assignee: 'Assignee',
}

/** Read `?sort=` into the table's shape, ignoring anything unrecognised. */
export function parseCaseSort(raw: string | null): TableSort | null {
  if (!raw) return null
  const descending = raw.startsWith('-')
  const key = descending ? raw.slice(1) : raw
  if (!CASE_SORT_KEYS.has(key)) return null
  return { key, direction: descending ? 'desc' : 'asc' }
}

/** The `?sort=` value a request ordering parameter is built from. */
export function caseSortParam(sort: TableSort | null): string {
  if (!sort) return ''
  return sort.direction === 'desc' ? `-${sort.key}` : sort.key
}

/**
 * Ascending, then descending, then back to the view's own default.
 *
 * The third state is not decoration. The default order is flagged-first and
 * then statutory due date -- the order that answers "what is late" -- and a
 * two-state control would make that order unreachable once an officer sorted
 * by anything else.
 */
export function nextCaseSort(current: TableSort | null, key: string): string {
  if (current?.key !== key) return key
  return current.direction === 'asc' ? `-${key}` : ''
}
