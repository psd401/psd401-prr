// ABOUTME: The single work-item detail screen and the shared work-row list.
// ABOUTME: They expose next-action metadata without copying requester or message content.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import { NoActionModal } from '../NoActionModal'
import {
  DISPOSITION_LABELS,
  type WorkItemDetail as WorkDetail,
  type WorkItemSummary,
} from '../work'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Select,
  Spinner,
  useToast,
} from '../ui'

function dispositionTone(disposition: WorkItemSummary['disposition']) {
  if (disposition === 'needs_attention') return 'amber' as const
  if (disposition === 'waiting') return 'blue' as const
  return 'neutral' as const
}

export function WorkItemRows({ rows }: { rows: WorkItemSummary[] }) {
  if (rows.length === 0) return <EmptyState>No work items in this queue.</EmptyState>
  return (
    <ol className="space-y-3">
      {rows.map((row) => (
        <li key={row.id}>
          <article className="rounded-xl border border-driftwood/60 bg-white p-4 shadow-sm">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="font-heading text-lg font-bold text-pacific">{row.next_action}</p>
                <p className="mt-1 text-sm text-pacific/80">
                  {row.request_number ?? 'No matched case'}
                  {row.assignee_username ? ` · ${row.assignee_username}` : ' · Unassigned'}
                </p>
              </div>
              <Badge tone={dispositionTone(row.disposition)}>
                {DISPOSITION_LABELS[row.disposition]}
              </Badge>
            </div>
            <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm">
              <span className="text-pacific/80">
                {row.urgency_date ? `Next due ${row.urgency_date}` : 'No current due date'}
              </span>
              <Link
                to={row.detail_url}
                className="rounded-lg font-heading font-semibold text-whulge underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge"
                aria-label={`Open ${row.next_action} for ${row.request_number ?? 'unmatched work'}`}
              >
                Open work item
              </Link>
            </div>
          </article>
        </li>
      ))}
    </ol>
  )
}

interface AssignmentTarget {
  id: number
  username: string
  display_name: string
}

function idempotencyKey(action: string) {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `work-detail:${action}:${random}`
}

function WorkItemActions({ work, roles }: { work: WorkDetail; roles: StaffRole[] }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [assignment, setAssignment] = useState(
    work.assignee_id === null ? '' : String(work.assignee_id),
  )
  const [noActionOpen, setNoActionOpen] = useState(false)
  const [noActionCategory, setNoActionCategory] = useState('informational_only')
  const [noActionReason, setNoActionReason] = useState('')

  useEffect(() => {
    setAssignment(work.assignee_id === null ? '' : String(work.assignee_id))
  }, [work.assignee_id])

  const targets = useQuery({
    queryKey: ['work-item-assignment-targets', work.id],
    queryFn: () => api<AssignmentTarget[]>(
      `/api/staff/work-items/${work.id}/assignment-targets/`,
    ),
  })

  async function refreshWork() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['work-item', work.id] }),
      queryClient.invalidateQueries({ queryKey: ['work-items'] }),
      queryClient.invalidateQueries({ queryKey: ['work-item-counts'] }),
      ...(work.request_number ? [
        queryClient.invalidateQueries({
          queryKey: ['case-workspace-summary', work.request_number],
        }),
      ] : []),
    ])
  }

  const metadata = useMutation({
    mutationFn: (action: 'read' | 'unread' | 'waiting' | 'handled' | 'reopen' | 'assignment') => {
      const base = {
        expected_version: work.version,
        idempotency_key: idempotencyKey(action),
      }
      if (action === 'assignment') {
        return api<WorkItemSummary>(`/api/staff/work-items/${work.id}/assignment/`, {
          method: 'POST',
          body: {
            ...base,
            assignee_id: assignment === '' ? null : Number(assignment),
          },
        })
      }
      if (action === 'read') {
        return api<WorkItemSummary>(`/api/staff/work-items/${work.id}/read/`, {
          method: 'POST',
          body: { idempotency_key: base.idempotency_key },
        })
      }
      if (action === 'unread') {
        return api<WorkItemSummary>(`/api/staff/work-items/${work.id}/unread/`, {
          method: 'POST',
          body: { idempotency_key: base.idempotency_key },
        })
      }
      if (action === 'reopen') {
        return api<WorkItemSummary>(`/api/staff/work-items/${work.id}/reopen/`, {
          method: 'POST',
          body: base,
        })
      }
      return api<WorkItemSummary>(`/api/staff/work-items/${work.id}/disposition/`, {
        method: 'POST',
        body: { ...base, disposition: action },
      })
    },
    onSuccess: async () => {
      await refreshWork()
      toast('success', 'Work metadata updated.')
    },
    onError: (error) => toast('error', error.message),
  })

  const noAction = useMutation({
    mutationFn: () => api<WorkItemSummary>(
      `/api/staff/work-items/${work.id}/no-action-required/`,
      {
        method: 'POST',
        body: {
          category: noActionCategory,
          reason: noActionReason,
          expected_version: work.version,
          idempotency_key: idempotencyKey('no-action'),
        },
      },
    ),
    onSuccess: async () => {
      setNoActionOpen(false)
      setNoActionReason('')
      await refreshWork()
      toast('success', 'No-action decision recorded.')
    },
    onError: (error) => toast('error', error.message),
  })

  const unresolved = work.disposition === 'needs_attention' || work.disposition === 'waiting'
  return (
    <>
      <Card title="Work actions" className="mt-5">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Assignment">
            <Select
              value={assignment}
              disabled={targets.isPending || metadata.isPending}
              onChange={(event) => setAssignment(event.target.value)}
            >
              <option value="">Unassigned</option>
              {targets.data?.map((target) => (
                <option key={target.id} value={target.id}>
                  {target.display_name || target.username}
                </option>
              ))}
            </Select>
          </Field>
          <Button
            type="button"
            variant="ghost"
            disabled={targets.isPending || metadata.isPending}
            onClick={() => metadata.mutate('assignment')}
          >
            Save assignment
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={metadata.isPending}
            onClick={() => metadata.mutate(work.is_read ? 'unread' : 'read')}
          >
            {work.is_read ? 'Mark unread' : 'Mark read'}
          </Button>
          {work.disposition === 'needs_attention' && (
            <Button type="button" variant="ghost" disabled={metadata.isPending} onClick={() => metadata.mutate('waiting')}>
              Set waiting
            </Button>
          )}
          {unresolved ? (
            <Button type="button" variant="ghost" disabled={metadata.isPending} onClick={() => metadata.mutate('handled')}>
              Mark handled
            </Button>
          ) : (
            <Button type="button" variant="ghost" disabled={metadata.isPending} onClick={() => metadata.mutate('reopen')}>
              Reopen team work
            </Button>
          )}
          {roles.includes('records_officer') && unresolved && (
            <Button type="button" variant="ghost" disabled={metadata.isPending || noAction.isPending} onClick={() => setNoActionOpen(true)}>
              No action required
            </Button>
          )}
        </div>
        {targets.isError && <div className="mt-3"><ErrorNote error={targets.error} /></div>}
        {metadata.isError && <div className="mt-3"><ErrorNote error={metadata.error} /></div>}
      </Card>
      <NoActionModal
        open={noActionOpen}
        onClose={() => setNoActionOpen(false)}
        onSubmit={() => noAction.mutate()}
        category={noActionCategory}
        onCategoryChange={setNoActionCategory}
        reason={noActionReason}
        onReasonChange={setNoActionReason}
        pending={noAction.isPending}
        error={noAction.isError ? noAction.error : null}
      />
    </>
  )
}

export function WorkItemDetail({ roles }: { roles: StaffRole[] }) {
  const { id = '' } = useParams()
  const work = useQuery({
    queryKey: ['work-item', id],
    queryFn: () => api<WorkDetail>(`/api/staff/work-items/${id}/`),
    enabled: id !== '',
  })

  if (work.isPending) return <Spinner label="Loading work item…" />
  if (work.isError) return <ErrorNote error={work.error} />
  if (!work.data) return <EmptyState>Work item not found.</EmptyState>
  const row = work.data
  return (
    <>
      <PageHeader title={row.next_action} subtitle={row.request_number ?? 'Unmatched work'} />
      <Card title="Work context">
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <div><dt className="font-heading font-semibold">Team state</dt><dd>{DISPOSITION_LABELS[row.disposition]}</dd></div>
          <div><dt className="font-heading font-semibold">Assignee</dt><dd>{row.assignee_username ?? 'Unassigned'}</dd></div>
          <div><dt className="font-heading font-semibold">Next due</dt><dd>{row.urgency_date ?? 'No current due date'}</dd></div>
          <div><dt className="font-heading font-semibold">Updated</dt><dd>{row.updated_at.slice(0, 16).replace('T', ' ')}</dd></div>
        </dl>
        {row.source_url && (
          <Link
            to={row.source_url}
            className="mt-5 inline-flex rounded-lg bg-cedar px-3.5 py-2 font-heading text-sm font-semibold text-white hover:bg-pacific focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge"
          >
            Open source context
          </Link>
        )}
      </Card>
      <WorkItemActions work={row} roles={roles} />
      <Card title="Work history" className="mt-5">
        {row.events.length === 0 ? (
          <EmptyState>No work events recorded.</EmptyState>
        ) : (
          <ol className="space-y-2">
            {row.events.map((event) => (
              <li key={event.id} className="border-b border-driftwood/40 pb-2 text-sm last:border-0">
                <span className="font-heading font-semibold">{event.event_type.replaceAll('_', ' ')}</span>
                {' · '}{event.actor_username ?? 'System'}{' · '}{event.occurred_at.slice(0, 16).replace('T', ' ')}
              </li>
            ))}
          </ol>
        )}
      </Card>
    </>
  )
}
