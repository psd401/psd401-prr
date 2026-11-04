// ABOUTME: Case-level redaction proposals (#40): pending proposals grouped by
// ABOUTME: exemption and pattern, sampled group acceptance, and one-click build.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { Badge, Button, Card, ErrorNote, Field, Select, Textarea } from '../ui'

interface ProposalGroup {
  exemption_id: number
  exemption_citation: string
  exemption_name: string
  requires_second_review: boolean
  pattern: string
  pattern_label: string
  pending: number
  records: number
}

interface Groups {
  mode: 'suggest' | 'batch'
  sample_size: number
  groups: ProposalGroup[]
  records_with_redactions: number
}

interface RedactionBatch {
  id: number
  exemption_citation: string
  pattern_label: string
  proposal_count: number
  samples: {
    id: string
    record_item_id: string
    original_filename: string
    page_number: number
    confidence: number
    rationale: string
  }[]
  outcome: 'open' | 'applied' | 'blocked'
  accepted_count: number
  skipped_count: number
}

interface Built {
  built: number
  awaiting_second_review: number
  already_current: number
  failed: number
}

type Judgment = { accept: boolean | null; reason: string }

function invalidate(qc: ReturnType<typeof useQueryClient>, request: string) {
  qc.invalidateQueries({ queryKey: ['redaction-groups', request] })
  qc.invalidateQueries({ queryKey: ['record-items'] })
}

function RedactionBatchPanel({
  batch,
  request,
  onOpen,
  onClose,
}: {
  batch: RedactionBatch
  request: string
  onOpen: (recordItemId: string) => void
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [judgments, setJudgments] = useState<Record<string, Judgment>>({})
  const decide = useMutation({
    mutationFn: () =>
      api<RedactionBatch>(`/api/staff/redaction-batches/${batch.id}/decide/`, {
        method: 'POST',
        body: {
          judgments: Object.fromEntries(
            Object.entries(judgments).map(([id, j]) => [id, { accept: j.accept, reason: j.reason }]),
          ),
        },
      }),
    onSuccess: () => invalidate(qc, request),
  })
  const decided = decide.data
  const complete = batch.samples.every((sample) => {
    const judgment = judgments[sample.id]
    return judgment && judgment.accept !== null && (judgment.accept || judgment.reason.trim().length >= 10)
  })
  const update = (id: string, patch: Partial<Judgment>) =>
    setJudgments((current) => ({
      ...current,
      [id]: { accept: null, reason: '', ...current[id], ...patch },
    }))
  return (
    <Card title={`Accept ${batch.pattern_label.toLowerCase()} under ${batch.exemption_citation}`} className="mb-5">
      {decided ? (
        <div role="status" className="space-y-3">
          {decided.outcome === 'applied' ? (
            <p className="text-sm text-ink">
              Accepted: {decided.accepted_count} redactions, each with its exemption citation, in your name.
              {decided.skipped_count > 0 && ` ${decided.skipped_count} could no longer be accepted and stay for one-at-a-time review.`}
            </p>
          ) : (
            <p className="text-sm text-danger-ink">
              Blocked: you rejected a sampled proposal. Your sample decisions were recorded; the rest of
              the group stays pending for one-at-a-time review.
            </p>
          )}
          <Button type="button" variant="secondary" onClick={onClose}>Done</Button>
        </div>
      ) : (
        <>
          <p className="mb-3 text-sm text-ink-muted">
            Check a random sample of {batch.samples.length} of {batch.proposal_count} proposals. If you accept
            every one, all {batch.proposal_count} become redactions. One rejection blocks the group.
          </p>
          <ul className="mb-3 space-y-3" aria-label="Sampled proposals">
            {batch.samples.map((sample) => (
              <li key={sample.id} className="rounded-lg border border-line px-3 py-2">
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    className="text-left text-sm font-semibold text-accent underline-offset-2 hover:underline"
                    onClick={() => onOpen(sample.record_item_id)}
                  >
                    {sample.original_filename}, page {sample.page_number}
                  </button>
                  <span className="text-sm text-ink-muted">{Math.round(sample.confidence * 100)}% confidence</span>
                  <Select
                    aria-label={`Decision for ${sample.original_filename}`}
                    className="w-auto"
                    value={judgments[sample.id]?.accept === undefined || judgments[sample.id]?.accept === null
                      ? ''
                      : judgments[sample.id]?.accept ? 'accept' : 'reject'}
                    onChange={(event) =>
                      update(sample.id, {
                        accept: event.target.value === '' ? null : event.target.value === 'accept',
                      })
                    }
                  >
                    <option value="">Decide this proposal…</option>
                    <option value="accept">Accept</option>
                    <option value="reject">Reject</option>
                  </Select>
                </div>
                <p className="mt-1 text-sm text-ink">{sample.rationale}</p>
                {judgments[sample.id]?.accept === false && (
                  <Field label="Why reject (at least 10 characters)">
                    <Textarea
                      rows={2}
                      value={judgments[sample.id]?.reason ?? ''}
                      onChange={(event) => update(sample.id, { reason: event.target.value })}
                    />
                  </Field>
                )}
              </li>
            ))}
          </ul>
          {decide.isError && <ErrorNote error={decide.error} />}
          <div className="flex flex-wrap gap-3">
            <Button type="button" disabled={!complete || decide.isPending} onClick={() => decide.mutate()}>
              Record decisions
            </Button>
            <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          </div>
        </>
      )}
    </Card>
  )
}

export function RedactionProposals({
  request,
  onOpen,
}: {
  request: string
  onOpen: (recordItemId: string) => void
}) {
  const qc = useQueryClient()
  const [batch, setBatch] = useState<RedactionBatch | null>(null)
  const open = useMutation({
    mutationFn: (group: ProposalGroup) =>
      api<RedactionBatch>('/api/staff/redaction-batches/', {
        method: 'POST',
        body: { request, exemption_id: group.exemption_id, pattern: group.pattern },
      }),
    onSuccess: setBatch,
  })
  const build = useMutation({
    mutationFn: () =>
      api<Built>('/api/staff/redaction-batches/build/', {
        method: 'POST',
        body: { request },
      }),
    onSuccess: () => invalidate(qc, request),
  })

  // Read last, below every mutation, so each API literal keeps its own method.
  const groups = useQuery({
    queryKey: ['redaction-groups', request],
    queryFn: () => api<Groups>(`/api/staff/redaction-batches/groups/?request=${encodeURIComponent(request)}`),
  })
  if (batch) {
    return (
      <RedactionBatchPanel batch={batch} request={request} onOpen={onOpen} onClose={() => setBatch(null)} />
    )
  }
  const data = groups.data
  // An unexpected payload hides the card rather than breaking the Records tab.
  const groupList = Array.isArray(data?.groups) ? data.groups : []
  const withRedactions = typeof data?.records_with_redactions === 'number' ? data.records_with_redactions : 0
  if (!data || (groupList.length === 0 && withRedactions === 0)) return null
  return (
    <Card title="Redaction proposals" className="mb-5">
      {groupList.length > 0 ? (
        <ul className="mb-4 space-y-2" aria-label="Redaction proposal groups">
          {groupList.map((group) => (
            <li
              key={`${group.exemption_id}:${group.pattern}`}
              className="flex flex-wrap items-center gap-3 rounded-lg border border-line px-3 py-2"
            >
              <span className="text-sm font-semibold text-ink">{group.pattern_label}</span>
              <span className="text-sm text-ink-muted">
                {group.exemption_citation} · {group.pending} proposals on {group.records} records
              </span>
              {group.requires_second_review && <Badge tone="amber">Second review required</Badge>}
              {data.mode === 'batch' && (
                <Button
                  type="button"
                  variant="secondary"
                  disabled={open.isPending}
                  onClick={() => open.mutate(group)}
                >
                  Accept all in group
                </Button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-4 text-sm text-ink-muted">No proposals are pending.</p>
      )}
      {open.isError && <ErrorNote error={open.error} />}
      {withRedactions > 0 && (
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" disabled={build.isPending} onClick={() => build.mutate()}>
            Build redacted PDFs
          </Button>
          {build.data && (
            <span role="status" className="text-sm text-ink">
              Built {build.data.built}. {build.data.awaiting_second_review} await second review.
              {build.data.already_current > 0 && ` ${build.data.already_current} already current.`}
              {build.data.failed > 0 && ` ${build.data.failed} could not be built; open them to see why.`}
            </span>
          )}
          {build.isError && <ErrorNote error={build.error} />}
        </div>
      )}
    </Card>
  )
}
