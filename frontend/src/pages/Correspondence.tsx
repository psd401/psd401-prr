// ABOUTME: Correspondence transport operations and the authorized outbound queue.
// ABOUTME: Requester-message work lives in Inbox and case Messages, never here.
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api, unwrap, type Page } from '../api/client'
import {
  CORRESPONDENCE_KIND_LABELS,
  CORRESPONDENCE_STATUS_LABELS,
  DELIVERY_STATUS_LABELS,
  correspondenceStatusTone,
  correspondenceTimestamp,
  type CorrespondenceSummary,
  type CorrespondenceOperations,
  type CorrespondenceReadiness,
} from '../correspondence'
import { PageHeader } from '../layout/AppLayout'
import { markBoundedInfiniteQueriesStale } from '../messages/boundedInfiniteCache'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Spinner,
  Table,
} from '../ui'

const TRANSPORT_FILTERS = [
  { value: '', label: 'All outbound' },
  { value: 'queued', label: 'Queued' },
  { value: 'failed', label: 'Failed' },
  { value: 'draft', label: 'Drafts' },
  { value: 'delivery_status_notification', label: 'Delivery status reports' },
] as const

function ageLabel(timestamp: string) {
  const milliseconds = Date.now() - new Date(timestamp).getTime()
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return 'just now'
  const minutes = Math.floor(milliseconds / 60_000)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

function nextPagePath<T>(page: Page<T> | T[]) {
  if (Array.isArray(page) || !page.next) return undefined
  if (/^https?:\/\//.test(page.next)) {
    const url = new URL(page.next)
    return `${url.pathname}${url.search}`
  }
  return page.next
}

function durationLabel(seconds: number | null) {
  if (seconds === null) return 'none'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

function componentLabel(
  component: CorrespondenceReadiness['checks'][number]['component'],
) {
  return component.replaceAll('_', ' ')
}

function operationCountLabel(label: string) {
  if (label === 'delivery_exceptions') {
    return 'Reported delivery exceptions (cumulative, unverified/advisory)'
  }
  return label.replaceAll('_', ' ')
}

export function Correspondence({ roles }: { roles: StaffRole[] }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [visiblePage, setVisiblePage] = useState(0)
  const pagingGeneration = useRef(0)
  const pagingInFlight = useRef<number | null>(null)
  const canViewOperations = roles.some((role) => (
    role === 'records_officer' || role === 'admin'
  ))
  const canViewTransportQueue = roles.includes('records_officer')
  const requestedFocus = searchParams.get('focus') ?? ''
  const focus = TRANSPORT_FILTERS.some((filter) => filter.value === requestedFocus)
    ? requestedFocus
    : ''
  const correspondenceListPath = focus === 'delivery_status_notification'
    ? '/api/staff/correspondence/?kind=delivery_status_notification'
    : focus
      ? `/api/staff/correspondence/?status=${encodeURIComponent(focus)}`
      : '/api/staff/correspondence/?direction=outbound'
  const list = useInfiniteQuery({
    queryKey: ['correspondence', 'transport', focus],
    initialPageParam: correspondenceListPath,
    queryFn: ({ pageParam, signal }) => api<Page<CorrespondenceSummary> | CorrespondenceSummary[]>(
      pageParam,
      { fetchAllPages: false, signal },
    ),
    getNextPageParam: nextPagePath,
    enabled: canViewTransportQueue,
    staleTime: Infinity,
  })
  const operations = useQuery({
    queryKey: ['correspondence-operations', 'summary'],
    queryFn: () => api<CorrespondenceOperations>(
      '/api/staff/correspondence-operations/',
    ),
    enabled: canViewOperations,
  })
  const readiness = useQuery({
    queryKey: ['correspondence-operations', 'readiness'],
    queryFn: () => api<CorrespondenceReadiness>(
      '/api/staff/correspondence-operations/readiness/',
    ),
    enabled: canViewOperations,
  })
  const pages = list.data?.pages ?? []
  const boundedPage = Math.min(visiblePage, Math.max(pages.length - 1, 0))
  const rows = pages[boundedPage] ? unwrap(pages[boundedPage]) : []
  const firstListPage = pages[0]
  const listTotal = firstListPage && !Array.isArray(firstListPage)
    ? firstListPage.count
    : rows.length
  const shownBefore = pages
    .slice(0, boundedPage)
    .reduce((count, page) => count + unwrap(page).length, 0)
  const shownFrom = rows.length > 0 ? shownBefore + 1 : 0
  const shownThrough = shownBefore + rows.length

  useEffect(() => {
    pagingGeneration.current += 1
    pagingInFlight.current = null
    setVisiblePage(0)
  }, [focus])

  useEffect(() => () => {
    markBoundedInfiniteQueriesStale(
      queryClient,
      ['correspondence', 'transport', focus],
    )
  }, [focus, queryClient])

  function selectFocus(value: string) {
    const next = new URLSearchParams(searchParams)
    if (value) next.set('focus', value)
    else next.delete('focus')
    setSearchParams(next)
  }

  async function showOlderPage() {
    if (boundedPage < pages.length - 1) {
      setVisiblePage(boundedPage + 1)
      return
    }
    const generation = pagingGeneration.current
    if (pagingInFlight.current === generation) return
    pagingInFlight.current = generation
    try {
      const result = await list.fetchNextPage()
      if (
        pagingGeneration.current !== generation
        || pagingInFlight.current !== generation
      ) return
      setVisiblePage(Math.max((result.data?.pages.length ?? 1) - 1, 0))
    } finally {
      if (pagingInFlight.current === generation) {
        pagingInFlight.current = null
      }
    }
  }

  return (
    <>
      <PageHeader
        title="Message operations"
        subtitle={canViewTransportQueue
          ? 'Delivery health, readiness, and the authorized outbound transport queue.'
          : 'Read-only delivery health and readiness without requester case data.'}
      />

      <div className="space-y-6">
        {canViewOperations && (
        <Card title="Operational summary">
          {(operations.isPending || readiness.isPending) && <Spinner />}
          {operations.isError && <ErrorNote error={operations.error} />}
          {readiness.isError && <ErrorNote error={readiness.error} />}
          {operations.data && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {Object.entries(operations.data.counts).map(([label, count]) => (
                  <div
                    key={label}
                    className="rounded-md border border-driftwood/60 bg-skylight p-3"
                  >
                    <p className="font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                      {operationCountLabel(label)}
                    </p>
                    <p className="mt-1 font-heading text-2xl font-bold text-pacific">
                      {count}
                    </p>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap gap-4 font-heading text-sm font-bold text-pacific">
                <span>
                  Oldest delivery: {
                    durationLabel(operations.data.ages_seconds.oldest_delivery)
                  }
                </span>
                <span>
                  Oldest claim: {
                    durationLabel(operations.data.ages_seconds.oldest_claim)
                  }
                </span>
                <span>
                  Oldest stuck: {
                    durationLabel(operations.data.ages_seconds.oldest_stuck)
                  }
                </span>
              </div>
              <div>
                <p className="mb-2 font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
                  Sanitized failure categories
                </p>
                {Object.keys(operations.data.failure_categories).length === 0
                  ? <p className="text-sm text-pacific/80">No current failures.</p>
                  : (
                      <div className="flex flex-wrap gap-2">
                        {Object.entries(operations.data.failure_categories).map(
                          ([category, count]) => (
                            <Badge key={category} tone="red">
                              {category}: {count}
                            </Badge>
                          ),
                        )}
                      </div>
                    )}
              </div>
            </div>
          )}
          {readiness.data && (
            <div className="mt-4 border-t border-driftwood/40 pt-4">
              <div className="mb-3 flex items-center gap-2">
                <span className="font-heading text-sm font-bold text-pacific">
                  Delivery readiness
                </span>
                <Badge tone={readiness.data.ready ? 'green' : 'red'}>
                  {readiness.data.ready ? 'ready' : 'not ready'}
                </Badge>
              </div>
              <div className="flex flex-wrap gap-2">
                {readiness.data.checks.map((check) => (
                  <span
                    key={check.component}
                    className="inline-flex items-center gap-2 rounded-md border border-driftwood/60 px-2.5 py-1.5 text-sm"
                  >
                    {componentLabel(check.component)}
                    <Badge tone={check.status === 'ready' ? 'green' : 'red'}>
                      {check.code}
                    </Badge>
                  </span>
                ))}
              </div>
            </div>
          )}
        </Card>
        )}

        {canViewTransportQueue && (
          <Card title="Transport queue">
            <div className="mb-5 flex flex-wrap gap-2">
              {TRANSPORT_FILTERS.map((filter) => (
                <Button
                  key={filter.value}
                  variant={focus === filter.value ? 'primary' : 'ghost'}
                  aria-pressed={focus === filter.value}
                  onClick={() => selectFocus(filter.value)}
                >
                  {filter.label}
                </Button>
              ))}
            </div>
            {list.isPending && <Spinner label="Loading transport queue…" />}
            {list.isError && <ErrorNote error={list.error} />}
            {list.isSuccess && rows.length === 0 && (
              <EmptyState>No correspondence matches this transport filter.</EmptyState>
            )}
            {rows.length > 0 && (
              <>
                <Table
                  columns={[
                    {
                      header: 'Request',
                      cell: (item: CorrespondenceSummary) => (
                        <Link
                          to={`/requests/${item.request_number}/messages?message=${item.id}`}
                          className="font-heading font-bold text-whulge underline-offset-2 hover:underline"
                        >
                          {item.request_number}
                        </Link>
                      ),
                    },
                    {
                      header: 'Direction',
                      cell: (item: CorrespondenceSummary) => (
                        <Badge tone={item.direction === 'inbound' ? 'blue' : 'green'}>
                          {item.direction}
                        </Badge>
                      ),
                    },
                    {
                      header: 'Kind',
                      cell: (item: CorrespondenceSummary) => (
                        CORRESPONDENCE_KIND_LABELS[item.kind]
                      ),
                    },
                    {
                      header: 'State',
                      cell: (item: CorrespondenceSummary) => (
                        <div className="space-y-1">
                          <Badge tone={correspondenceStatusTone(item.status)}>
                            {CORRESPONDENCE_STATUS_LABELS[item.status] ?? item.status}
                          </Badge>
                          {item.delivery_status && (
                            <p className="text-xs text-pacific/80">
                              {DELIVERY_STATUS_LABELS[item.delivery_status]
                                ?? item.delivery_status}
                            </p>
                          )}
                        </div>
                      ),
                    },
                    {
                      header: 'Failure',
                      cell: (item: CorrespondenceSummary) => item.failure_category || '—',
                    },
                    {
                      header: 'Age',
                      cell: (item: CorrespondenceSummary) => (
                        ageLabel(correspondenceTimestamp(item))
                      ),
                    },
                  ]}
                  rows={rows}
                  rowKey={(item) => item.id}
                />
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <span className="text-sm text-pacific/80">
                    Showing {shownFrom}–{shownThrough} of {listTotal} messages
                  </span>
                  {boundedPage > 0 && (
                    <Button
                      variant="ghost"
                      onClick={() => setVisiblePage(Math.max(boundedPage - 1, 0))}
                    >
                      Show newer messages
                    </Button>
                  )}
                  {(boundedPage < pages.length - 1 || list.hasNextPage) && (
                    <Button
                      variant="ghost"
                      onClick={() => void showOlderPage()}
                      disabled={list.isFetchingNextPage}
                    >
                      {list.isFetchingNextPage ? 'Loading more…' : 'Load more messages'}
                    </Button>
                  )}
                </div>
              </>
            )}
          </Card>
        )}
      </div>
    </>
  )
}
