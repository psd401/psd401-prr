// ABOUTME: Records tab "Group by cluster" view (#38): every filtered record in
// ABOUTME: clusters by thread, sender or list, tag, or AI label band.
import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { Badge, Button, Card, EmptyState, ErrorNote, Select, Spinner } from '../ui'
import { BatchReviewPanel } from './BatchReview'
import { type ReviewBatch, useTrustPolicy } from './trustPolicy'

interface ClusterSample {
  id: string
  original_filename: string
  review_state: string
  advisory_suggested_state: string | null
  advisory_confidence: number | null
}

interface RecordCluster {
  key: string
  label: string
  size: number
  ai_majority: { label: string; count: number } | null
  samples: ClusterSample[]
  record_item_ids: string[]
}

interface ClusterResponse {
  group_by: string
  cluster_count: number
  clusters: RecordCluster[]
}

const CLUSTER_OPTIONS = [
  { value: 'thread', label: 'Email thread' },
  { value: 'sender', label: 'Sender or mailing list' },
  { value: 'tag', label: 'Tag' },
  { value: 'ai_label', label: 'AI label and confidence' },
]

const SUGGESTION_TEXT: Record<string, string> = {
  responsive: 'Responsive',
  non_responsive: 'Non-responsive',
  uncertain: 'Uncertain',
}

function majorityText(cluster: RecordCluster): string {
  if (!cluster.ai_majority) return 'No AI suggestions'
  const { label, count } = cluster.ai_majority
  return `AI majority: ${SUGGESTION_TEXT[label] ?? label} (${count} of ${cluster.size})`
}

export function RecordClusters({
  filters,
  filterKey,
  clusterBy,
  onClusterBy,
  onOpen,
}: {
  filters: URLSearchParams
  filterKey: readonly string[]
  clusterBy: string
  onClusterBy: (value: string) => void
  onOpen: (recordItemId: string) => void
}) {
  const clusters = useQuery({
    queryKey: ['record-item-clusters', ...filterKey, clusterBy],
    queryFn: () => {
      const params = new URLSearchParams(filters)
      params.set('group_by', clusterBy)
      return api<ClusterResponse>(`/api/staff/record-items/clusters/?${params.toString()}`)
    },
    placeholderData: (previous) => previous,
  })
  const data = clusters.data
  const [batch, setBatch] = useState<ReviewBatch | null>(null)
  const open = useMutation({
    mutationFn: (input: { cluster: RecordCluster; state: string }) =>
      api<ReviewBatch>('/api/staff/review-batches/', {
        method: 'POST',
        body: {
          record_item_ids: input.cluster.record_item_ids,
          state: input.state,
          cluster_label: input.cluster.label,
        },
      }),
    onSuccess: setBatch,
  })
  const trust = useTrustPolicy(filters.get('request') ?? '')
  const batchStates = (['responsive', 'non_responsive'] as const).filter(
    (state) => trust.data && trust.data[state] !== 'suggest',
  )
  if (batch) {
    return <BatchReviewPanel batch={batch} onOpen={onOpen} onClose={() => setBatch(null)} />
  }
  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Select
          aria-label="Cluster by"
          className="w-auto"
          value={clusterBy}
          onChange={(event) => onClusterBy(event.target.value)}
        >
          {CLUSTER_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </Select>
        <span className="text-sm text-ink-muted">
          Clusters cover every record these filters match, not just one page.
          A cluster is a way to look at records; it decides nothing.
        </span>
      </div>
      {open.isError && <ErrorNote error={open.error} />}
      {clusters.isPending && <Spinner />}
      {clusters.isError && <ErrorNote error={clusters.error} />}
      {data && data.clusters.length === 0 && (
        <EmptyState>No record items match these filters.</EmptyState>
      )}
      {data && data.cluster_count > data.clusters.length && (
        <p className="mb-3 text-sm text-ink-muted">
          Showing the {data.clusters.length} largest of {data.cluster_count} clusters.
        </p>
      )}
      <ul className="space-y-3" aria-label="Record clusters">
        {data?.clusters.map((cluster) => (
          <li key={cluster.key || 'none'} className="rounded-lg border border-line px-3 py-2">
            <div className="flex flex-wrap items-baseline gap-2">
              <h3 className="font-heading text-sm font-semibold text-ink">{cluster.label}</h3>
              <span className="text-sm text-ink-muted">
                {cluster.size} {cluster.size === 1 ? 'record' : 'records'}
              </span>
              <Badge tone={cluster.ai_majority ? 'blue' : 'neutral'}>{majorityText(cluster)}</Badge>
              {batchStates.map((state) => (
                <Button
                  key={state}
                  type="button"
                  variant="secondary"
                  disabled={open.isPending}
                  onClick={() => open.mutate({ cluster, state })}
                >
                  Approve cluster as {SUGGESTION_TEXT[state]?.toLowerCase()}
                </Button>
              ))}
            </div>
            <ul className="mt-1 text-sm" aria-label={`Sample records in ${cluster.label}`}>
              {cluster.samples.map((sample) => (
                <li key={sample.id}>
                  <button
                    type="button"
                    className="text-left font-semibold text-accent underline-offset-2 hover:underline"
                    onClick={() => onOpen(sample.id)}
                  >
                    {sample.original_filename || sample.id}
                  </button>
                  {sample.advisory_suggested_state && (
                    <span className="ml-2 text-ink-muted">
                      AI: {SUGGESTION_TEXT[sample.advisory_suggested_state] ?? sample.advisory_suggested_state}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </Card>
  )
}
