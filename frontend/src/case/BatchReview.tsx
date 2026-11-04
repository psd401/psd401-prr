// ABOUTME: Trust-ladder batch review (#39): judge a random sample before a
// ABOUTME: cluster or the Ready-to-discard pile is decided in one action.
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { Button, Card, ErrorNote, Select } from '../ui'
import { type ReviewBatch, useTrustPolicy } from './trustPolicy'

const STATE_TEXT = { responsive: 'Responsive', non_responsive: 'Non-responsive' }

function invalidateRecords(qc: ReturnType<typeof useQueryClient>) {
  for (const key of ['record-items', 'record-item-facets', 'record-item-clusters', 'review-trust-policy']) {
    qc.invalidateQueries({ queryKey: [key] })
  }
}

/** Judge every sampled record; the batch applies only if all agree. */
export function BatchReviewPanel({
  batch,
  onOpen,
  onClose,
}: {
  batch: ReviewBatch
  onOpen: (recordItemId: string) => void
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [judgments, setJudgments] = useState<Record<string, string>>({})
  const decide = useMutation({
    mutationFn: () =>
      api<ReviewBatch>(`/api/staff/review-batches/${batch.id}/decide/`, {
        method: 'POST',
        body: { judgments },
      }),
    onSuccess: () => invalidateRecords(qc),
  })
  const decided = decide.data
  const proposed = STATE_TEXT[batch.proposed_state]
  const complete = batch.samples.every((sample) => judgments[sample.id])
  const title = batch.kind === 'discard'
    ? `Elusion check: ${batch.record_count} records ready to discard`
    : `Approve ${batch.label || 'cluster'} as ${proposed.toLowerCase()}`
  return (
    <Card title={title} className="mb-5">
      {decided ? (
        <div role="status" className="space-y-3">
          {decided.outcome === 'applied' ? (
            <p className="text-sm text-ink">
              Applied: {decided.applied_count} records marked {proposed.toLowerCase()} in your name.
              {decided.skipped_count > 0 && ` ${decided.skipped_count} reviewed meanwhile were left as they were.`}
            </p>
          ) : (
            <p className="text-sm text-danger-ink">
              Blocked: at least one sampled record was not {proposed.toLowerCase()}. Your sample
              judgments were recorded; the other records stay unreviewed for per-record review.
            </p>
          )}
          <Button type="button" variant="secondary" onClick={onClose}>Done</Button>
        </div>
      ) : (
        <>
          <p className="mb-3 text-sm text-ink-muted">
            {batch.kind === 'discard'
              ? `Check a random sample of ${batch.samples.length} from the pile for anything responsive. If none is, all ${batch.record_count} are marked non-responsive in your name.`
              : `Judge a random sample of ${batch.samples.length} of ${batch.record_count} records. If every one is ${proposed.toLowerCase()}, all ${batch.record_count} are marked ${proposed.toLowerCase()} in your name. One disagreement blocks the batch.`}
          </p>
          <ul className="mb-3 space-y-2" aria-label="Sampled records">
            {batch.samples.map((sample) => (
              <li key={sample.id} className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  className="text-left text-sm font-semibold text-accent underline-offset-2 hover:underline"
                  onClick={() => onOpen(sample.id)}
                >
                  {sample.original_filename || sample.id}
                </button>
                <Select
                  aria-label={`Judgment for ${sample.original_filename || sample.id}`}
                  className="w-auto"
                  value={judgments[sample.id] ?? ''}
                  onChange={(event) =>
                    setJudgments((current) => ({ ...current, [sample.id]: event.target.value }))
                  }
                >
                  <option value="">Judge this record…</option>
                  <option value="responsive">Responsive</option>
                  <option value="non_responsive">Non-responsive</option>
                </Select>
              </li>
            ))}
          </ul>
          {decide.isError && <ErrorNote error={decide.error} />}
          <div className="flex flex-wrap gap-3">
            <Button type="button" disabled={!complete || decide.isPending} onClick={() => decide.mutate()}>
              Record judgments
            </Button>
            <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          </div>
        </>
      )}
    </Card>
  )
}

/** The Ready-to-discard pile, shown only when non-responsive is ``auto``. */
export function DiscardBatchCard({
  request,
  onOpen,
}: {
  request: string
  onOpen: (recordItemId: string) => void
}) {
  const [batch, setBatch] = useState<ReviewBatch | null>(null)
  const start = useMutation({
    mutationFn: () =>
      api<ReviewBatch>('/api/staff/review-batches/discard/', {
        method: 'POST',
        body: { request },
      }),
    onSuccess: setBatch,
  })
  const trust = useTrustPolicy(request)
  if (batch) return <BatchReviewPanel batch={batch} onOpen={onOpen} onClose={() => setBatch(null)} />
  const data = trust.data
  if (!data || data.non_responsive !== 'auto' || data.discard_candidates === 0) return null
  return (
    <Card title="Ready to discard" className="mb-5">
      <p className="mb-3 text-sm text-ink-muted">
        {data.discard_candidates} unreviewed records the AI calls non-responsive with at least{' '}
        {Math.round(data.min_confidence * 100)}% confidence. Nothing is applied until you check a random
        sample of {Math.min(data.elusion_sample_size, data.discard_candidates)} and find none responsive.
      </p>
      {start.isError && <ErrorNote error={start.error} />}
      <Button type="button" disabled={start.isPending} onClick={() => start.mutate()}>
        Start elusion check
      </Button>
    </Card>
  )
}
