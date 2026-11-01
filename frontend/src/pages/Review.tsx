// ABOUTME: Reviewer-focused role-scoped work queue.
// ABOUTME: It reuses PII-minimal work summaries and exact work-item navigation.
import { useQuery } from '@tanstack/react-query'
import { api, unwrap, type Page } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import type { WorkItemSummary } from '../work'
import { ErrorNote, Spinner } from '../ui'
import { WorkItemRows } from './WorkItem'

export function Review() {
  const work = useQuery({
    queryKey: ['work-items', 'review'],
    queryFn: () => api<Page<WorkItemSummary> | WorkItemSummary[]>(
      '/api/staff/work-items/?queue=review',
      { fetchAllPages: false },
    ),
  })

  return (
    <>
      <PageHeader title="Review" subtitle="Record and second-review actions ordered by current due evidence." />
      {work.isPending && <Spinner label="Loading review work…" />}
      {work.isError && <ErrorNote error={work.error} />}
      {work.data && <WorkItemRows rows={unwrap(work.data)} />}
    </>
  )
}
