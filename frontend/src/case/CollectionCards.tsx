// ABOUTME: The Collect stage's lead: one card per collection with its source,
// ABOUTME: state, record counts, and its actions on the card itself (#45).
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api } from '../api/client'
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Spinner, Textarea, useToast } from '../ui'
import { type CollectionCardTask, useCaseCollections } from './caseCollections'


const STATE_LABELS: Record<string, string> = {
  pending: 'Pending',
  running: 'Running',
  ingesting: 'Ingesting export',
  complete: 'Complete',
  failed: 'Failed',
}

const STATE_TONES: Record<string, 'neutral' | 'blue' | 'green' | 'red'> = {
  pending: 'neutral',
  running: 'blue',
  ingesting: 'blue',
  complete: 'green',
  failed: 'red',
}

// The service refuses a shorter reason; the button stays disabled until then.
const MIN_SUPERSEDE_REASON = 20

/** The exact effect, stated before the officer confirms (#28). */
function supersedeCountText(count: number) {
  return `This takes ${count} unreviewed ${count === 1 ? 'record' : 'records'} out of review.`
}

function CollectionCard({
  number,
  task,
  canManage,
}: {
  number: string
  task: CollectionCardTask
  canManage: boolean
}) {
  const qc = useQueryClient()
  const toast = useToast()
  const [reason, setReason] = useState<string | null>(null)
  const supersede = useMutation({
    mutationFn: (text: string) => api<{ record_count: number }>(
      `/api/staff/collection-tasks/${task.id}/supersede/`,
      { method: 'POST', body: { reason: text } },
    ),
    onSuccess: (result) => {
      setReason(null)
      qc.invalidateQueries({ queryKey: ['collection-tasks'] })
      qc.invalidateQueries({ queryKey: ['record-items'] })
      qc.invalidateQueries({ queryKey: ['record-item-facets'] })
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
      qc.invalidateQueries({ queryKey: ['case-closure-readiness', number] })
      toast(
        'success',
        result.record_count === 0
          ? 'No unreviewed records were left to supersede.'
          : `${result.record_count} unreviewed records left review.`,
      )
    },
  })
  // The same condition the task dialog applies: only a finished Gmail export
  // has records the service can take back out of review.
  const canSupersede = canManage && task.kind === 'vault_mail' && task.state === 'complete'
  const viewRecords = `/requests/${number}/review?view=records&collection_task=${task.id}`

  return (
    <li className="rounded-lg border border-line bg-surface-raised p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-heading text-base font-bold text-ink">{task.source_label}</p>
          {task.search_terms && (
            <p className="mt-1 text-xs text-ink-muted">Terms: {task.search_terms}</p>
          )}
        </div>
        <Badge tone={STATE_TONES[task.state] ?? 'neutral'}>
          {STATE_LABELS[task.state] ?? task.state}
        </Badge>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-ink-muted">Records created</dt>
          <dd className="font-heading text-lg font-bold text-ink">{task.records_created}</dd>
        </div>
        <div>
          <dt className="text-ink-muted">Still unreviewed</dt>
          <dd className="font-heading text-lg font-bold text-ink">{task.records_unreviewed}</dd>
        </div>
      </dl>
      <div className="mt-3 flex flex-wrap gap-2">
        <Link
          to={viewRecords}
          className="rounded-md border border-line px-3 py-1.5 text-sm font-semibold text-accent hover:bg-surface-hover"
        >
          View records
        </Link>
        {canSupersede && reason === null && (
          <Button variant="ghost" onClick={() => setReason('')}>Supersede</Button>
        )}
        {canManage && (
          <Link
            to={`/requests/${number}/scope`}
            className="rounded-md border border-line px-3 py-1.5 text-sm font-semibold text-accent hover:bg-surface-hover"
          >
            Re-scope
          </Link>
        )}
      </div>
      {canSupersede && reason !== null && (
        <div className="mt-3 space-y-3 rounded-md border border-line p-3">
          <p className="text-sm font-semibold text-ink">
            {supersedeCountText(task.records_unreviewed)}
          </p>
          <p className="text-sm text-ink-muted">
            Takes every unreviewed record from this export out of review. Nothing is
            deleted, and records already decided are not changed. The reason is kept in
            the audit log and case timeline; do not name students or include record contents.
          </p>
          <Field label="Reason">
            <Textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={3} />
          </Field>
          {supersede.isError && <ErrorNote error={supersede.error} />}
          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => setReason(null)}>Cancel</Button>
            <Button
              onClick={() => supersede.mutate(reason.trim())}
              disabled={supersede.isPending || reason.trim().length < MIN_SUPERSEDE_REASON}
            >
              Supersede unreviewed records
            </Button>
          </div>
        </div>
      )}
    </li>
  )
}

export function CollectionCards({ number, roles }: { number: string; roles: StaffRole[] }) {
  const collections = useCaseCollections(number)
  if (collections.isPending) return <Spinner label="Loading collections…" />
  if (collections.isError) return <ErrorNote error={collections.error} />
  return (
    <Card title="Collections">
      {collections.data.length === 0 ? (
        <EmptyState>No collection has started for this case.</EmptyState>
      ) : (
        <ul className="grid gap-3 lg:grid-cols-2" aria-label="Collections">
          {collections.data.map((task) => (
            <CollectionCard
              key={task.id}
              number={number}
              task={task}
              canManage={roles.includes('records_officer')}
            />
          ))}
        </ul>
      )}
    </Card>
  )
}
