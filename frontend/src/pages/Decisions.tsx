// ABOUTME: Decision queue (#43): batches an agent or person proposed, each
// ABOUTME: approved or rejected here in one click with its evidence beside it.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Select, Spinner, Textarea } from '../ui'

type Kind = 'review' | 'supersede' | 'scope_and_collect' | 'redaction_group' | 'intake_label'
type State = 'proposed' | 'approved' | 'rejected'

interface DecisionBatch {
  id: string
  kind: Kind
  request_number: string
  payload: Record<string, unknown>
  evidence_summary: string
  proposed_by_username: string
  created_at: string
  state: State
  decided_by_username: string | null
  decided_at: string | null
  decision_reason: string
  result: Record<string, unknown>
  records: { id: string; original_filename: string; review_state: string }[]
}

const KIND_LABELS: Record<Kind, string> = {
  review: 'Review records',
  supersede: 'Supersede a collection',
  scope_and_collect: 'Collect a scope',
  redaction_group: 'Redaction group',
  intake_label: 'Intake label',
}

/** The case tab where a batch's evidence lives. */
const KIND_TABS: Record<Kind, string> = {
  review: 'records',
  supersede: 'collection',
  scope_and_collect: 'collection',
  redaction_group: 'review',
  intake_label: 'overview',
}

const MIN_REJECTION_REASON = 10

/** One line saying what approving would do. */
function proposalText(batch: DecisionBatch) {
  const p = batch.payload
  switch (batch.kind) {
    case 'review': {
      const count = Array.isArray(p.record_item_ids) ? p.record_item_ids.length : 0
      const state = String(p.state).replace('_', '-')
      return `Mark ${count} record${count === 1 ? '' : 's'} ${state}.`
    }
    case 'supersede':
      return `Supersede collection task ${String(p.collection_task_id)}'s unreviewed records: ${String(p.reason)}`
    case 'scope_and_collect':
      return p.rescope_reason
        ? `Re-scope to count ${String(p.count_run_id)} and supersede the current collection: ${String(p.rescope_reason)}`
        : `Start the Vault collection of count ${String(p.count_run_id)}.`
    case 'redaction_group':
      return `Open the sampled check for the ${String(p.pattern) || 'all'}-pattern redactions under exemption ${String(p.exemption_id)}; a reviewer judges the samples in the Review tab.`
    case 'intake_label':
      return p.label === 'duplicate_of'
        ? `Merge this case into ${String(p.target_case_number)}: ${String(p.reason)}`
        : `Close as not a records request (no requester mail): ${String(p.reason)}`
  }
}

function BatchCard({ batch }: { batch: DecisionBatch }) {
  const qc = useQueryClient()
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const refresh = () => qc.invalidateQueries({ queryKey: ['decision-batches'] })
  const approve = useMutation({
    mutationFn: () => api<DecisionBatch>(`/api/staff/decision-batches/${batch.id}/approve/`, {
      method: 'POST',
      body: {},
    }),
    onSuccess: refresh,
  })
  const reject = useMutation({
    mutationFn: () => api<DecisionBatch>(`/api/staff/decision-batches/${batch.id}/reject/`, {
      method: 'POST',
      body: { reason },
    }),
    onSuccess: refresh,
  })
  const busy = approve.isPending || reject.isPending
  return (
    <Card title={KIND_LABELS[batch.kind]} className="mb-4">
      <div className="space-y-3 text-sm text-ink">
        <p className="flex flex-wrap items-center gap-2">
          <Link
            className="font-heading font-semibold text-accent underline-offset-2 hover:underline"
            to={`/requests/${batch.request_number}/${KIND_TABS[batch.kind]}`}
          >
            {batch.request_number}
          </Link>
          <span className="text-ink-muted">
            Proposed by {batch.proposed_by_username} on {batch.created_at.slice(0, 10)}
          </span>
          {batch.state !== 'proposed' && (
            <Badge tone={batch.state === 'approved' ? 'green' : 'neutral'}>
              {batch.state === 'approved' ? 'Approved' : 'Rejected'} by {batch.decided_by_username}
            </Badge>
          )}
        </p>
        <p>{proposalText(batch)}</p>
        <details>
          <summary className="cursor-pointer font-semibold">Evidence</summary>
          <p className="mt-2 whitespace-pre-wrap">{batch.evidence_summary}</p>
          {batch.records.length > 0 && (
            <ul className="mt-2 list-disc pl-5 text-ink-muted" aria-label="Records in this batch">
              {batch.records.map((record) => (
                <li key={record.id}>
                  {record.original_filename || record.id} — now {record.review_state.replace('_', '-')}
                </li>
              ))}
            </ul>
          )}
        </details>
        {batch.state === 'rejected' && batch.decision_reason && (
          <p className="text-ink-muted">Reason: {batch.decision_reason}</p>
        )}
        {approve.isError && <ErrorNote error={approve.error} />}
        {reject.isError && <ErrorNote error={reject.error} />}
        {batch.state === 'proposed' && (
          rejecting ? (
            <div className="space-y-2">
              <Field label={`Why reject it (at least ${MIN_REJECTION_REASON} characters)`}>
                <Textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
              </Field>
              <div className="flex gap-2">
                <Button
                  type="button"
                  disabled={reason.trim().length < MIN_REJECTION_REASON || busy}
                  onClick={() => reject.mutate()}
                >
                  Reject batch
                </Button>
                <Button type="button" variant="secondary" onClick={() => setRejecting(false)}>Cancel</Button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <Button type="button" disabled={busy} onClick={() => approve.mutate()}>Approve</Button>
              <Button type="button" variant="secondary" disabled={busy} onClick={() => setRejecting(true)}>
                Reject
              </Button>
            </div>
          )
        )}
      </div>
    </Card>
  )
}

export function Decisions() {
  const [state, setState] = useState<State>('proposed')
  const batches = useQuery({
    queryKey: ['decision-batches', state],
    queryFn: () => api<Page<DecisionBatch> | DecisionBatch[]>(`/api/staff/decision-batches/?state=${state}`),
  })
  const rows = batches.data ? unwrap(batches.data) : []
  return (
    <>
      <PageHeader
        title="Decisions"
        subtitle="Batches an agent or colleague proposed. Approving runs the same action as doing it by hand, in your name."
      />
      <div className="mb-4 max-w-xs">
        <Field label="Show">
          <Select value={state} onChange={(event) => setState(event.target.value as State)}>
            <option value="proposed">Waiting for a decision</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
          </Select>
        </Field>
      </div>
      {batches.isPending && <Spinner label="Loading decision batches…" />}
      {batches.isError && <ErrorNote error={batches.error} />}
      {batches.isSuccess && rows.length === 0 && (
        <EmptyState>
          {state === 'proposed' ? 'Nothing is waiting for a decision.' : `No ${state} batches.`}
        </EmptyState>
      )}
      {rows.map((batch) => <BatchCard key={batch.id} batch={batch} />)}
    </>
  )
}
