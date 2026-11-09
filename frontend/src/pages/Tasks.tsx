// ABOUTME: Collection tasks screen — custodian search assignments per request,
// ABOUTME: filterable by state/urgency, with inline state edits and a detail modal.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import type { StaffRole } from '../api/auth'
import { pacificDateString } from '../date'
import { isVaultKind } from '../vaultSources'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  FilterChip,
  Input,
  Modal,
  Select,
  Spinner,
  Table,
  Textarea,
  useToast,
} from '../ui'
import { formatDay } from '../date'

interface CollectionTask {
  id: number
  /** "automatic" when the scope trust policy started it (#37). */
  trigger?: 'manual' | 'automatic'
  request: number
  request_number: string
  system_name: string | null
  system_slug: string | null
  system_urgent_hold: boolean | null
  kind: string
  state: string
  instructions: string
  custodian_emails: string[]
  search_terms: string
  date_range_start: string | null
  date_range_end: string | null
  assigned_to_id: number | null
  assigned_to_username: string | null
  due_date: string | null
  vault_matter_id: string
  vault_hold_id: string
  vault_export_id: string
  vault_ingest: VaultIngestProgress | null
  /** Unreviewed records this collection created, for the supersede confirm. */
  records_unreviewed?: number
  created_at: string
  completed_at: string | null
}

interface VaultIngestProgress {
  state: string
  attempt_count: number
  files_total: number
  files_processed: number
  messages_seen: number
  items_created: number
  duplicates_skipped: number
  last_error_code: string
  started_at: string | null
  updated_at: string
  completed_at: string | null
}

interface SupersedeResult {
  collection_task_id: number
  record_count: number
}

// The service refuses a shorter reason; the button stays disabled until then.
const MIN_SUPERSEDE_REASON = 20

interface TaskPatchPayload {
  id: number
  state?: string
  due_date?: string | null
  assigned_to_id?: number | null
}

interface StaffUserChoice {
  id: number
  username: string
  display_name: string
}

interface RequestScope {
  id: number
  number: string
}

interface TaskRecordSystem {
  id: number
  name: string
  custodian_email: string
  default_assignee_id: number | null
  search_checklist: string
}

const emptyManualTaskForm = {
  system: '',
  assignedTo: '',
  dueDate: '',
  instructions: '',
  custodianEmails: '',
  searchTerms: '',
}

const STATES = [
  { value: 'pending', label: 'Pending' },
  { value: 'running', label: 'Running' },
  { value: 'ingesting', label: 'Ingesting export', disabled: true },
  { value: 'complete', label: 'Complete' },
  { value: 'failed', label: 'Failed' },
]

const STATE_TABS = [{ value: '', label: 'All' }, ...STATES]

const KIND_LABELS: Record<string, string> = {
  vault_mail: 'Vault — Gmail',
  vault_drive: 'Vault — Drive',
  vault_chat: 'Vault — Chat',
  manual: 'Manual / Department',
}

function isPastDue(dueDate: string | null, state: string): boolean {
  if (!dueDate || state === 'complete') return false
  return dueDate < pacificDateString()
}

function isVaultTask(task: CollectionTask): boolean {
  return isVaultKind(task.kind)
}

export function Tasks({
  roles,
  requestNumber: scopedRequestNumber,
}: {
  roles: StaffRole[]
  requestNumber?: string
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const requestFilter = scopedRequestNumber ?? searchParams.get('request')?.trim() ?? ''
  const taskTarget = searchParams.get('task')?.trim() ?? ''
  const requestNumber = requestFilter
  const qc = useQueryClient()
  const canManageTask = roles.includes('records_officer')
  const canUpdateState = canManageTask || roles.includes('liaison')
  const toast = useToast()
  const activeStateFilter = searchParams.get('task_state') ?? ''
  const activeUrgentOnly = searchParams.get('task_urgent') === 'true'
  const [selectedTask, setSelected] = useState<CollectionTask | null>(null)
  // ?task= is the source of truth for the open dialog. The router can commit
  // the URL after this state, or never, when Back supersedes the push.
  const selected = selectedTask && String(selectedTask.id) === taskTarget ? selectedTask : null
  const [dueDateDraft, setDueDateDraft] = useState('')
  const [assignDraft, setAssignDraft] = useState('')
  const [creating, setCreating] = useState(false)
  const [manualTaskForm, setManualTaskForm] = useState(emptyManualTaskForm)
  const [supersedeReason, setSupersedeReason] = useState<string | null>(null)
  const hydratedTarget = useRef('')

  const list = useQuery({
    queryKey: ['collection-tasks', requestFilter, activeStateFilter, activeUrgentOnly],
    queryFn: () => {
      const params = new URLSearchParams()
      if (requestFilter) params.set('request', requestFilter)
      if (activeStateFilter) params.set('state', activeStateFilter)
      if (activeUrgentOnly) params.set('urgent', 'true')
      const qs = params.toString()
      return api<Page<CollectionTask> | CollectionTask[]>(
        `/api/staff/collection-tasks/${qs ? `?${qs}` : ''}`,
      )
    },
  })
  const targetedTask = useQuery({
    queryKey: ['collection-task-target', requestFilter, taskTarget],
    queryFn: () => api<CollectionTask>(
      `/api/staff/collection-tasks/${taskTarget}/${requestFilter ? `?request=${encodeURIComponent(requestFilter)}` : ''}`,
    ),
    enabled: taskTarget !== '',
  })

  useEffect(() => {
    if (!taskTarget) {
      hydratedTarget.current = ''
      setSelected(null)
      return
    }
    const targetKey = `${requestFilter}:${taskTarget}`
    if (hydratedTarget.current === targetKey) return
    const task = targetedTask.data
    if (!task || (requestFilter && task.request_number !== requestFilter)) return
    hydratedTarget.current = targetKey
    setSelected(task)
    setDueDateDraft(task.due_date ?? '')
    setAssignDraft(
      task.assigned_to_id != null
        ? String(task.assigned_to_id)
        : '',
    )
  }, [requestFilter, targetedTask.data, taskTarget])
  const staffUsers = useQuery({
    queryKey: ['staff-users', 'task-assignees'],
    queryFn: () =>
      api<Page<StaffUserChoice> | StaffUserChoice[]>('/api/staff/staff-users/'),
    enabled: canManageTask,
  })
  const requestScope = useQuery({
    queryKey: ['request', requestFilter],
    queryFn: () => api<RequestScope>(`/api/staff/requests/${requestNumber}/`),
    enabled: canManageTask && requestFilter !== '',
  })
  const recordSystems = useQuery({
    queryKey: ['record-systems', 'task-assignment'],
    queryFn: () => api<Page<TaskRecordSystem> | TaskRecordSystem[]>(
      '/api/staff/record-systems/',
    ),
    enabled: canManageTask && requestFilter !== '',
  })

  const patchTask = useMutation({
    mutationFn: ({ id, ...body }: TaskPatchPayload) =>
      api<CollectionTask>(`/api/staff/collection-tasks/${id}/`, { method: 'PATCH', body }),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ['collection-tasks'] })
      if (requestFilter) {
        qc.invalidateQueries({ queryKey: ['case-workspace-summary', requestFilter] })
        qc.invalidateQueries({ queryKey: ['case-closure-readiness', requestFilter] })
      }
      setSelected((cur) => (cur && cur.id === updated.id ? updated : cur))
      toast('success', 'Task updated.')
    },
    onError: (err) => toast('error', err.message),
  })
  const supersedeTask = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) =>
      api<SupersedeResult>(`/api/staff/collection-tasks/${id}/supersede/`, {
        method: 'POST',
        body: { reason },
      }),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ['collection-tasks'] })
      qc.invalidateQueries({ queryKey: ['record-items'] })
      qc.invalidateQueries({ queryKey: ['record-item-facets'] })
      if (requestFilter) {
        qc.invalidateQueries({ queryKey: ['case-workspace-summary', requestFilter] })
        qc.invalidateQueries({ queryKey: ['case-closure-readiness', requestFilter] })
      }
      setSupersedeReason(null)
      toast(
        'success',
        result.record_count === 0
          ? 'No unreviewed records were left to supersede.'
          : `${result.record_count} unreviewed records left review.`,
      )
    },
    onError: (err) => toast('error', err.message),
  })
  const createManualTask = useMutation({
    mutationFn: () => {
      if (!requestScope.data) throw new Error('The request is still loading.')
      const custodianEmails = manualTaskForm.custodianEmails
        .split(/[\s,]+/)
        .map((email) => email.trim())
        .filter(Boolean)
      return api<CollectionTask>('/api/staff/collection-tasks/', {
        method: 'POST',
        body: {
          request: requestScope.data.id,
          system: Number(manualTaskForm.system),
          instructions: manualTaskForm.instructions.trim(),
          assigned_to_id: manualTaskForm.assignedTo === ''
            ? null
            : Number(manualTaskForm.assignedTo),
          due_date: manualTaskForm.dueDate || null,
          custodian_emails: custodianEmails,
          search_terms: manualTaskForm.searchTerms.trim(),
        },
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['collection-tasks'] })
      if (requestFilter) {
        qc.invalidateQueries({ queryKey: ['case-workspace-summary', requestFilter] })
        qc.invalidateQueries({ queryKey: ['case-closure-readiness', requestFilter] })
      }
      setCreating(false)
      setManualTaskForm(emptyManualTaskForm)
      toast('success', 'Search assignment created.')
    },
    onError: (err) => toast('error', err.message),
  })

  function openCreateTask() {
    setManualTaskForm(emptyManualTaskForm)
    createManualTask.reset()
    setCreating(true)
  }

  function selectRecordSystem(value: string) {
    const system = recordSystems.data
      ? unwrap(recordSystems.data).find((candidate) => String(candidate.id) === value)
      : undefined
    setManualTaskForm((current) => ({
      ...current,
      system: value,
      assignedTo: system?.default_assignee_id == null
        ? current.assignedTo
        : String(system.default_assignee_id),
      custodianEmails: system?.custodian_email ?? current.custodianEmails,
      instructions: system?.search_checklist || current.instructions,
    }))
  }

  function submitManualTask(event: FormEvent) {
    event.preventDefault()
    createManualTask.mutate()
  }

  function openTask(row: CollectionTask) {
    setSelected(row)
    setDueDateDraft(row.due_date ?? '')
    setAssignDraft(row.assigned_to_id != null ? String(row.assigned_to_id) : '')
    const next = new URLSearchParams(searchParams)
    next.set('task', String(row.id))
    setSearchParams(next)
  }

  function closeTask() {
    setSupersedeReason(null)
    setSelected(null)
    const next = new URLSearchParams(searchParams)
    next.delete('task')
    setSearchParams(next)
  }

  function changeStateFilter(value: string) {
    const next = new URLSearchParams(searchParams)
    if (value) next.set('task_state', value)
    else next.delete('task_state')
    setSearchParams(next)
  }

  function changeUrgentOnly(value: boolean) {
    const next = new URLSearchParams(searchParams)
    if (value) next.set('task_urgent', 'true')
    else next.delete('task_urgent')
    setSearchParams(next)
  }

  function commitDueDate(value: string) {
    setDueDateDraft(value)
    if (selected) patchTask.mutate({ id: selected.id, due_date: value === '' ? null : value })
  }

  function saveAssignment() {
    if (!selected) return
    const trimmed = assignDraft.trim()
    if (trimmed === '') {
      if (selected.assigned_to_id !== null) patchTask.mutate({ id: selected.id, assigned_to_id: null })
      return
    }
    const parsed = Number(trimmed)
    if (!Number.isInteger(parsed)) {
      toast('error', 'The selected assignee is invalid.')
      return
    }
    if (parsed !== selected.assigned_to_id) patchTask.mutate({ id: selected.id, assigned_to_id: parsed })
  }

  if (list.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />
  if (targetedTask.isError) return <ErrorNote error={targetedTask.error} />

  const rows = unwrap(list.data)

  return (
    <>
      {canManageTask && requestFilter && (
        <div className="mb-4 flex justify-end">
          <Button
            type="button"
            onClick={openCreateTask}
            disabled={requestScope.isPending || requestScope.isError}
          >
            Add search assignment
          </Button>
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Task state filters" className="flex flex-wrap gap-2">
          {STATE_TABS.map((tab) => (
            <FilterChip
              key={tab.value}
              selected={activeStateFilter === tab.value}
              onClick={() => changeStateFilter(tab.value)}
            >
              {tab.label}
            </FilterChip>
          ))}
        </div>
        <label
          className={`flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1.5 font-heading text-sm font-semibold transition-colors ${
            activeUrgentOnly
              ? 'border-danger bg-danger-soft text-danger-ink'
              : 'border-line bg-surface-raised text-ink hover:bg-surface-sunken'
          }`}
        >
          <input
            type="checkbox"
            className="size-3.5"
            checked={activeUrgentOnly}
            onChange={(e) => changeUrgentOnly(e.target.checked)}
          />
          Urgent only
        </label>
      </div>

      <Card title="Collection tasks">
        {rows.length === 0 ? (
          <EmptyState>No collection tasks match the current filters.</EmptyState>
        ) : (
          <Table
            columns={[
              { header: 'System', cell: (r: CollectionTask) => r.system_name ?? '—' },
              { header: 'Kind', cell: (r: CollectionTask) => KIND_LABELS[r.kind] ?? r.kind },
              {
                header: 'State',
                cell: (r: CollectionTask) => (
                  <div onClick={(e) => e.stopPropagation()}>
                    {canUpdateState && !isVaultTask(r) ? (
                      <Select
                        aria-label={`State for ${r.system_name ?? r.request_number}`}
                        value={r.state}
                        onChange={(e) => patchTask.mutate({ id: r.id, state: e.target.value })}
                      >
                        {STATES.map((s) => (
                          <option key={s.value} value={s.value} disabled={s.disabled}>
                            {s.label}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      STATES.find((state) => state.value === r.state)?.label ?? r.state
                    )}
                  </div>
                ),
              },
              {
                header: 'Urgent',
                cell: (r: CollectionTask) =>
                  r.system_urgent_hold ? <Badge tone="red">URGENT</Badge> : '—',
              },
              {
                header: 'Started',
                cell: (r: CollectionTask) =>
                  r.trigger === 'automatic' ? <Badge tone="amber">Automatically</Badge> : 'By staff',
              },
              {
                header: 'Due date',
                cell: (r: CollectionTask) =>
                  r.due_date ? (
                    <span className={isPastDue(r.due_date, r.state) ? 'font-bold text-danger-ink' : ''}>
                      {formatDay(r.due_date)}
                    </span>
                  ) : (
                    '—'
                  ),
              },
              {
                header: 'Assigned',
                cell: (r: CollectionTask) => r.assigned_to_username ?? '—',
              },
              {
                header: 'Completed',
                cell: (r: CollectionTask) => formatDay(r.completed_at),
              },
            ]}
            rows={rows}
            rowKey={(r) => r.id}
            onRowClick={openTask}
          />
        )}
      </Card>

      <Modal
        open={creating}
        title={`Add search assignment — ${requestFilter}`}
        onClose={() => setCreating(false)}
      >
        <form onSubmit={submitManualTask} className="space-y-4">
          <Field label="Record system">
            <Select
              value={manualTaskForm.system}
              onChange={(event) => selectRecordSystem(event.target.value)}
              required
              disabled={recordSystems.isPending || recordSystems.isError}
            >
              <option value="">Choose a system</option>
              {recordSystems.data && unwrap(recordSystems.data).map((system) => (
                <option key={system.id} value={system.id}>{system.name}</option>
              ))}
            </Select>
          </Field>
          <Field label="Assign to">
            <Select
              value={manualTaskForm.assignedTo}
              onChange={(event) => setManualTaskForm((current) => ({
                ...current,
                assignedTo: event.target.value,
              }))}
              disabled={staffUsers.isPending || staffUsers.isError}
            >
              <option value="">Unassigned</option>
              {staffUsers.data && unwrap(staffUsers.data).map((user) => (
                <option key={user.id} value={user.id}>
                  {user.display_name} ({user.username})
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Due date">
            <Input
              type="date"
              value={manualTaskForm.dueDate}
              onChange={(event) => setManualTaskForm((current) => ({
                ...current,
                dueDate: event.target.value,
              }))}
            />
          </Field>
          <Field label="Instructions">
            <Textarea
              value={manualTaskForm.instructions}
              onChange={(event) => setManualTaskForm((current) => ({
                ...current,
                instructions: event.target.value,
              }))}
              required
              rows={4}
            />
          </Field>
          <Field label="Custodian accounts">
            <Textarea
              value={manualTaskForm.custodianEmails}
              onChange={(event) => setManualTaskForm((current) => ({
                ...current,
                custodianEmails: event.target.value,
              }))}
              rows={2}
              placeholder="name@psd401.net"
            />
          </Field>
          <Field label="Search terms">
            <Textarea
              value={manualTaskForm.searchTerms}
              onChange={(event) => setManualTaskForm((current) => ({
                ...current,
                searchTerms: event.target.value,
              }))}
              rows={3}
            />
          </Field>
          {createManualTask.isError && <ErrorNote error={createManualTask.error} />}
          {recordSystems.isError && <ErrorNote error={recordSystems.error} />}
          {requestScope.isError && <ErrorNote error={requestScope.error} />}
          <div className="flex justify-end gap-3">
            <Button type="button" variant="ghost" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createManualTask.isPending}>
              Create assignment
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={selected !== null}
        title={selected ? `Task — request #${selected.request_number}` : ''}
        onClose={closeTask}
      >
        {selected && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <Badge>{KIND_LABELS[selected.kind] ?? selected.kind}</Badge>
              {selected.trigger === 'automatic' && <Badge tone="amber">Started automatically</Badge>}
              {selected.system_name && <Badge tone="blue">{selected.system_name}</Badge>}
              {selected.system_urgent_hold && <Badge tone="red">URGENT</Badge>}
            </div>

            <div>
              <p className="font-heading text-sm font-bold text-ink">Instructions</p>
              <p className="mt-1 whitespace-pre-wrap text-sm text-ink">
                {selected.instructions || '—'}
              </p>
            </div>

            <div>
              <p className="font-heading text-sm font-bold text-ink">Custodian emails</p>
              <p className="mt-1 text-sm text-ink">
                {selected.custodian_emails.length ? selected.custodian_emails.join(', ') : '—'}
              </p>
            </div>

            <div>
              <p className="font-heading text-sm font-bold text-ink">Search terms</p>
              <p className="mt-1 text-sm text-ink">{selected.search_terms || '—'}</p>
            </div>

            {selected.vault_ingest && (
              <section className="rounded-xl border border-line bg-surface-sunken p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-heading text-sm font-bold text-ink">
                    Vault ingest progress
                  </p>
                  <Badge tone={selected.vault_ingest.state === 'failed' ? 'red' : 'blue'}>
                    {selected.vault_ingest.state}
                  </Badge>
                </div>
                <div className="mt-3 grid gap-2 text-sm text-ink sm:grid-cols-2">
                  <p>
                    {selected.vault_ingest.files_processed} of {selected.vault_ingest.files_total}{' '}
                    files processed
                  </p>
                  <p>{selected.vault_ingest.messages_seen} messages inspected</p>
                  <p>{selected.vault_ingest.items_created} review items created</p>
                  <p>
                    {selected.vault_ingest.duplicates_skipped} duplicates retained as evidence
                  </p>
                </div>
                <p className="mt-3 text-xs text-ink-muted">
                  Attempt {selected.vault_ingest.attempt_count}
                  {selected.vault_ingest.last_error_code
                    ? ` · ${selected.vault_ingest.last_error_code}`
                    : ''}
                </p>
              </section>
            )}

            {canManageTask && isVaultKind(selected.kind) && selected.state === 'complete' && (
              <section className="rounded-xl border border-line p-4">
                <p className="font-heading text-sm font-bold text-ink">
                  Supersede this collection
                </p>
                <p className="mt-1 text-sm text-ink-muted">
                  Takes every unreviewed record from this export out of review. Nothing is
                  deleted, and records already decided are not changed. A later collection
                  that finds the same message puts it back in review.
                </p>
                {supersedeReason === null ? (
                  <div className="mt-3 flex justify-end">
                    <Button type="button" variant="ghost" onClick={() => setSupersedeReason('')}>
                      Supersede collection
                    </Button>
                  </div>
                ) : (
                  <div className="mt-3 space-y-3">
                    {typeof selected.records_unreviewed === 'number' && (
                      <p className="text-sm font-semibold text-ink">
                        This takes {selected.records_unreviewed} unreviewed{' '}
                        {selected.records_unreviewed === 1 ? 'record' : 'records'} out of review.
                      </p>
                    )}
                    <p className="text-sm text-ink-muted">
                      The reason is kept in the audit log and case timeline. Describe the
                      scope problem; do not name students or include record contents.
                    </p>
                    <Field label="Reason">
                      <Textarea
                        value={supersedeReason}
                        onChange={(event) => setSupersedeReason(event.target.value)}
                        rows={3}
                      />
                    </Field>
                    {supersedeTask.isError && <ErrorNote error={supersedeTask.error} />}
                    <div className="flex justify-end gap-3">
                      <Button type="button" variant="ghost" onClick={() => setSupersedeReason(null)}>
                        Cancel
                      </Button>
                      <Button
                        type="button"
                        onClick={() => supersedeTask.mutate({
                          id: selected.id,
                          reason: supersedeReason.trim(),
                        })}
                        disabled={
                          supersedeTask.isPending
                          || supersedeReason.trim().length < MIN_SUPERSEDE_REASON
                        }
                      >
                        Supersede unreviewed records
                      </Button>
                    </div>
                  </div>
                )}
              </section>
            )}

            {canManageTask ? (
              <div className="grid grid-cols-2 gap-4">
                <Field label="Due date">
                  <Input
                    type="date"
                    value={dueDateDraft}
                    onChange={(e) => commitDueDate(e.target.value)}
                  />
                </Field>
                <Field label="Assign to">
                  <Select
                    value={assignDraft}
                    onChange={(e) => setAssignDraft(e.target.value)}
                    disabled={staffUsers.isPending || staffUsers.isError}
                  >
                    <option value="">Unassigned</option>
                    {staffUsers.data && unwrap(staffUsers.data).map((user) => (
                      <option key={user.id} value={user.id}>
                        {user.display_name} ({user.username})
                      </option>
                    ))}
                  </Select>
                </Field>
                <div className="col-start-2">
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={saveAssignment}
                    disabled={patchTask.isPending || assignDraft === (
                      selected.assigned_to_id == null ? '' : String(selected.assigned_to_id)
                    )}
                  >
                    Save assignment
                  </Button>
                </div>
              </div>
            ) : (
              <p className="text-sm text-ink-muted">
                Due {formatDay(selected.due_date)} · Assigned {selected.assigned_to_username ?? '—'}
              </p>
            )}

            {patchTask.isError && <ErrorNote error={patchTask.error} />}
            {staffUsers.isError && <ErrorNote error={staffUsers.error} />}
          </div>
        )}
      </Modal>
    </>
  )
}
