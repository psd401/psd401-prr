// ABOUTME: The one running log of a case, rendered full or bounded.
// ABOUTME: Overview and the Activity tab must never tell different stories.

import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { Card, EmptyState, ErrorNote, Spinner } from '../ui'
import { boundedActivity, type ActivityEntry } from './activity'

function ActivityList({ entries }: { entries: ActivityEntry[] }) {
  return (
    <ol className="space-y-4 border-l-2 border-seaglass/40 pl-4">
      {entries.map((entry) => (
        <li key={entry.id} className="relative">
          <span
            className={
              'absolute -left-[21px] top-1 size-2.5 rounded-full '
              + (entry.flagged ? 'bg-red-600' : 'bg-seaglass')
            }
          />
          <p className="text-xs text-pacific/80">
            {entry.occurred_at.slice(0, 16).replace('T', ' ')} · {entry.type}
          </p>
          <p className="font-heading text-sm font-bold text-pacific">
            {entry.label}
          </p>
          {entry.detail && (
            <p
              className={
                'mt-1 whitespace-pre-wrap text-sm '
                + (entry.flagged
                  ? 'rounded-md border border-red-300 bg-red-50 p-2 text-red-950'
                  : 'text-pacific/80')
              }
            >
              {entry.detail}
            </p>
          )}
          {/* Absent means "withheld from this role", not "performed by the
              system". The server sends the literal "system" when an officer
              may see that no person acted; anyone else gets no byline rather
              than invented provenance. */}
          {entry.actor_username && (
            <p className="mt-1 text-xs text-pacific/80">
              By {entry.actor_username}
            </p>
          )}
        </li>
      ))}
    </ol>
  )
}

/**
 * The case's running log.
 *
 * Overview used to render its own five-event list from the request payload's
 * raw StatusEvents while the Activity tab rendered a different, server-merged
 * feed. Two histories of one case that could disagree is worse than either.
 * Both now read this component and this endpoint; only `limit` differs.
 */
export function CaseActivityFeed({
  number,
  limit,
  title,
}: {
  number: string
  limit?: number
  title?: string
}) {
  const activity = useQuery({
    queryKey: ['case-activity', number],
    queryFn: () => api<{ results: ActivityEntry[] }>(
      `/api/staff/requests/${number}/activity/`,
      { fetchAllPages: false },
    ),
  })
  if (activity.isPending) return <Spinner label="Loading case activity…" />
  if (activity.isError) return <ErrorNote error={activity.error} />

  const all = activity.data?.results ?? []
  if (all.length === 0) {
    const empty = (
      <EmptyState>No role-visible case activity has been recorded.</EmptyState>
    )
    return title ? <Card title={title} className="lg:col-span-2">{empty}</Card> : empty
  }

  const entries = limit === undefined ? all : boundedActivity(all, limit)

  if (!title) {
    return (
      <Card>
        <ActivityList entries={entries} />
      </Card>
    )
  }
  return (
    <Card title={title} className="lg:col-span-2">
      <ActivityList entries={entries} />
    </Card>
  )
}
