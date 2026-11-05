// ABOUTME: Collection-tab card for iterative scoping (#36): every counted query,
// ABOUTME: why the agent changed it, the final proposed scope, and a re-run.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { Badge, Button, Card, ErrorNote, Field, Textarea } from '../ui'

interface Iteration {
  number: number
  count_run_id: string
  input_sha256: string
  reason: string
  query: string
  search_scope: 'accounts' | 'entire_org'
  custodians: string[]
  state: string
  total: number | null
  top_accounts: { email: string; count: number }[]
  date_range_start: string | null
  date_range_end: string | null
}

interface Session {
  id: number
  trigger: 'automatic' | 'manual'
  actor: string | null
  reason: string
  state: 'running' | 'complete' | 'failed'
  stop_reason: string
  max_iterations: number
  iterations: Iteration[]
  final: Iteration | null
}

function scopeText(iteration: Iteration) {
  return iteration.search_scope === 'accounts'
    ? `${iteration.custodians.length} named mailbox${iteration.custodians.length === 1 ? '' : 'es'}`
    : 'all district mailboxes'
}

function totalText(iteration: Iteration) {
  if (iteration.total !== null) return `${iteration.total.toLocaleString()} messages`
  return iteration.state === 'failed' ? 'count failed' : 'counting…'
}

interface CurrentCollection {
  task_id: number
  state: string
  trigger: 'manual' | 'automatic'
  count_run_id: string | null
  total: number | null
}

export function ScopingTrail({ number }: { number: string }) {
  const qc = useQueryClient()
  const [reason, setReason] = useState('')
  const [open, setOpen] = useState(false)
  const [rescopeReason, setRescopeReason] = useState('')
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['scoping', number] })
    qc.invalidateQueries({ queryKey: ['collection-tasks'] })
  }
  const collect = useMutation({
    mutationFn: (scope: Iteration) =>
      api(`/api/staff/requests/${number}/vault-collection/`, {
        method: 'POST',
        body: { count_run_id: scope.count_run_id, expected_input_sha256: scope.input_sha256, confirmed: true },
      }),
    onSuccess: refresh,
  })
  const rescope = useMutation({
    mutationFn: (scope: Iteration) =>
      api(`/api/staff/requests/${number}/rescope/`, {
        method: 'POST',
        body: {
          count_run_id: scope.count_run_id,
          expected_input_sha256: scope.input_sha256,
          reason: rescopeReason,
        },
      }),
    onSuccess: () => {
      setRescopeReason('')
      refresh()
    },
  })
  const rerun = useMutation({
    mutationFn: () =>
      api(`/api/staff/requests/${number}/scoping/`, {
        method: 'POST',
        body: { reason },
      }),
    onSuccess: () => {
      setReason('')
      setOpen(false)
      qc.invalidateQueries({ queryKey: ['scoping', number] })
    },
  })

  // Read below the mutation so each API literal keeps its own method.
  const latest = useQuery({
    queryKey: ['scoping', number],
    queryFn: () =>
      api<{ session: Session | null; current_collection: CurrentCollection | null }>(
        `/api/staff/requests/${number}/scoping/`,
      ),
    refetchInterval: (query) => (query.state.data?.session?.state === 'running' ? 4000 : false),
  })
  const session = latest.data?.session ?? null
  const collection = latest.data?.current_collection ?? null
  const final = session?.state === 'complete' ? session.final : null
  const collectsFinal = Boolean(final && collection?.count_run_id === final.count_run_id)
  return (
    <Card title="Scope iterations">
      {latest.isError && <ErrorNote error={latest.error} />}
      {!session && latest.isSuccess && (
        <p className="mb-3 text-sm text-ink-muted">
          No scoping has run for this request yet. It starts from the intent proposal.
        </p>
      )}
      {session && (
        <>
          <p className="mb-3 text-sm text-ink-muted">
            {session.trigger === 'manual'
              ? `Re-run by ${session.actor ?? 'an officer'}: ${session.reason}`
              : 'Run automatically from the intent proposal.'}{' '}
            Up to {session.max_iterations} counts. Counts only — nothing is held or exported.
          </p>
          <ol className="mb-4 space-y-3" aria-label="Scope iterations">
            {session.iterations.map((iteration) => (
              <li key={iteration.number} className="rounded-lg border border-line px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-heading text-sm font-semibold text-ink">Count {iteration.number}</span>
                  <Badge tone={iteration.state === 'failed' ? 'red' : 'neutral'}>{totalText(iteration)}</Badge>
                  <span className="text-sm text-ink-muted">{scopeText(iteration)}</span>
                </div>
                <p className="mt-1 text-sm text-ink">{iteration.reason}</p>
                <code className="mt-1 block break-words text-xs text-ink-muted">{iteration.query}</code>
                {iteration.top_accounts.length > 0 && (
                  <p className="mt-1 text-xs text-ink-muted">
                    Most matches: {iteration.top_accounts.map((a) => `${a.email} (${a.count})`).join(', ')}
                  </p>
                )}
              </li>
            ))}
          </ol>
          <div role="status" className="mb-4 text-sm text-ink">
            {session.state === 'running' && 'Scoping is still counting.'}
            {session.state !== 'running' && session.final && (
              <>
                <span className="font-semibold">Proposed scope:</span> count {session.final.number},{' '}
                {totalText(session.final)} in {scopeText(session.final)}. {session.stop_reason}
              </>
            )}
            {session.state === 'failed' && !session.final && `Scoping stopped: ${session.stop_reason}`}
          </div>
          {final && !collection && (
            <div className="mb-4 space-y-2">
              <Button type="button" disabled={collect.isPending} onClick={() => collect.mutate(final)}>
                Approve and collect
              </Button>
              <p className="text-sm text-ink-muted">
                Starts the Vault export of this scope in your name
                {final.search_scope === 'accounts' ? ', with a hold on the named mailboxes' : ''}.
              </p>
              {collect.isError && <ErrorNote error={collect.error} />}
            </div>
          )}
          {collection && collectsFinal && (
            <p className="mb-4 text-sm text-ink" role="note">
              Collecting this scope ({collection.state})
              {collection.trigger === 'automatic' ? ', started automatically by the trust policy' : ''}.
            </p>
          )}
          {final && collection && !collectsFinal && (
            <div className="mb-4 space-y-2 rounded-lg border border-line px-3 py-2">
              <p className="text-sm text-ink">
                <span className="font-semibold">Re-scope:</span> the current collection has{' '}
                {collection.total?.toLocaleString() ?? 'an unknown number of'} messages; the proposed scope
                has {totalText(final)}.
                {collection.state !== 'complete' && ' Re-scoping waits until the current collection finishes.'}
              </p>
              <Field label="Why change the scope (at least 20 characters)">
                <Textarea rows={2} value={rescopeReason} onChange={(event) => setRescopeReason(event.target.value)} />
              </Field>
              {rescope.isError && <ErrorNote error={rescope.error} />}
              <Button
                type="button"
                disabled={rescopeReason.trim().length < 20 || rescope.isPending || collection.state !== 'complete'}
                onClick={() => rescope.mutate(final)}
              >
                Approve re-scope
              </Button>
              <p className="text-sm text-ink-muted">
                Collects the proposed scope and supersedes the current collection&apos;s unreviewed records.
              </p>
            </div>
          )}
        </>
      )}
      {open ? (
        <div className="space-y-2">
          <Field label="Why run scoping again (at least 10 characters)">
            <Textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
          </Field>
          {rerun.isError && <ErrorNote error={rerun.error} />}
          <div className="flex gap-2">
            <Button type="button" disabled={reason.trim().length < 10 || rerun.isPending} onClick={() => rerun.mutate()}>
              Run scoping
            </Button>
            <Button type="button" variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <Button type="button" variant="secondary" onClick={() => setOpen(true)}>Re-run scoping</Button>
      )}
    </Card>
  )
}
