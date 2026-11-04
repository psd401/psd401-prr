// ABOUTME: System registry screen — every district record system, its
// ABOUTME: custodian, retention rule, and search checklist, with a punch list.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { api, unwrap, type Page } from '../api/client'
import type { StaffRole } from '../api/auth'
import { PageHeader } from '../layout/AppLayout'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Modal,
  Select,
  Spinner,
  Table,
  Textarea,
  useToast,
} from '../ui'

interface RecordSystem {
  id: number
  slug: string
  name: string
  category: string
  vendor: string
  integration: string
  custodian_title: string
  custodian_email: string
  default_assignee_id: number | null
  default_assignee_username: string | null
  retention_days: number | null
  retention_note: string
  urgent_hold: boolean
  triage_keywords: string[]
  search_checklist: string
  needs_verification: boolean
  active: boolean
}

interface StaffUserChoice {
  id: number
  username: string
  display_name: string
}

const emptyCreateForm = {
  name: '',
  category: '',
  custodian_title: '',
  custodian_email: '',
  default_assignee_id: '',
  keywordsText: '',
  change_reason: '',
}

const emptyEditForm = {
  custodian_title: '',
  custodian_email: '',
  default_assignee_id: '',
  retention_days: '',
  retention_note: '',
  needs_verification: false,
  active: true,
  change_reason: '',
}

function formatRetention(row: RecordSystem): string {
  if (row.retention_days != null) return `${row.retention_days} days`
  if (row.retention_note) {
    return row.retention_note.length > 40
      ? `${row.retention_note.slice(0, 40)}…`
      : row.retention_note
  }
  return '—'
}

function CustodianEmailCell({
  system,
  onSave,
}: {
  system: RecordSystem
  onSave: (id: number, email: string) => void
}) {
  const [value, setValue] = useState(system.custodian_email)

  useEffect(() => {
    setValue(system.custodian_email)
  }, [system.custodian_email])

  return (
    <Input
      type="email"
      aria-label={`Custodian email for ${system.name}`}
      value={value}
      className="text-xs"
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => {
        if (value !== system.custodian_email) onSave(system.id, value)
      }}
    />
  )
}

export function Registry({ roles }: { roles: StaffRole[] }) {
  const qc = useQueryClient()
  const canEdit = roles.includes('records_officer')
  const toast = useToast()
  const [creating, setCreating] = useState(false)
  const [createForm, setCreateForm] = useState(emptyCreateForm)
  const [editing, setEditing] = useState<RecordSystem | null>(null)
  const [editForm, setEditForm] = useState(emptyEditForm)

  const list = useQuery({
    queryKey: ['record-systems'],
    queryFn: () => api<Page<RecordSystem> | RecordSystem[]>('/api/staff/record-systems/'),
  })

  const staffUsers = useQuery({
    queryKey: ['staff-users'],
    queryFn: () => api<Page<StaffUserChoice> | StaffUserChoice[]>('/api/staff/staff-users/'),
    enabled: canEdit,
  })

  const patchEmail = useMutation({
    mutationFn: ({ id, custodian_email }: { id: number; custodian_email: string }) =>
      api<RecordSystem>(`/api/staff/record-systems/${id}/`, {
        method: 'PATCH',
        body: { custodian_email },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['record-systems'] })
      toast('success', 'Custodian assigned')
    },
    onError: (err) => toast('error', err instanceof Error ? err.message : 'Update failed'),
  })

  const create = useMutation({
    mutationFn: (payload: typeof emptyCreateForm) =>
      api<RecordSystem>('/api/staff/record-systems/', {
        method: 'POST',
        body: {
          name: payload.name,
          category: payload.category,
          custodian_title: payload.custodian_title,
          custodian_email: payload.custodian_email,
          default_assignee_id: payload.default_assignee_id === ''
            ? null
            : Number(payload.default_assignee_id),
          triage_keywords: payload.keywordsText
            .split(',')
            .map((k) => k.trim())
            .filter(Boolean),
          change_reason: payload.change_reason,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['record-systems'] })
      toast('success', 'System added.')
      closeCreate()
    },
  })

  const update = useMutation({
    mutationFn: (payload: typeof emptyEditForm) => {
      if (!editing) throw new Error('No system selected')
      return api<RecordSystem>(`/api/staff/record-systems/${editing.id}/`, {
        method: 'PATCH',
        body: {
          custodian_title: payload.custodian_title,
          custodian_email: payload.custodian_email,
          default_assignee_id: payload.default_assignee_id === ''
            ? null
            : Number(payload.default_assignee_id),
          retention_days: payload.retention_days === '' ? null : Number(payload.retention_days),
          retention_note: payload.retention_note,
          needs_verification: payload.needs_verification,
          active: payload.active,
          change_reason: payload.change_reason,
        },
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['record-systems'] })
      toast('success', 'System updated.')
      closeEdit()
    },
  })

  function openCreate() {
    setCreateForm(emptyCreateForm)
    setCreating(true)
  }

  function closeCreate() {
    setCreating(false)
    create.reset()
  }

  function openEdit(row: RecordSystem) {
    setEditing(row)
    setEditForm({
      custodian_title: row.custodian_title,
      custodian_email: row.custodian_email,
      default_assignee_id: row.default_assignee_id == null
        ? ''
        : String(row.default_assignee_id),
      retention_days: row.retention_days == null ? '' : String(row.retention_days),
      retention_note: row.retention_note,
      needs_verification: row.needs_verification,
      active: row.active,
      change_reason: '',
    })
  }

  function closeEdit() {
    setEditing(null)
    update.reset()
  }

  function submitCreate(e: FormEvent) {
    e.preventDefault()
    create.mutate(createForm)
  }

  function submitEdit(e: FormEvent) {
    e.preventDefault()
    update.mutate(editForm)
  }

  if (list.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />

  const rows = unwrap(list.data)
  const assigneeChoices = staffUsers.data ? unwrap(staffUsers.data) : []
  const punchList = [...rows]
    .filter((r) => r.needs_verification)
    .sort((a, b) => Number(b.urgent_hold) - Number(a.urgent_hold) || a.name.localeCompare(b.name))

  return (
    <>
      <PageHeader
        title="System registry"
        subtitle="Every district system, its custodian, and how to search it."
        actions={canEdit ? <Button onClick={openCreate}>Add system</Button> : undefined}
      />

      {punchList.length > 0 && (
        <Card title="Verification punch list" className="mb-6 border-warning">
          <ul className="divide-y divide-line">
            {punchList.map((sys) => (
              <li
                key={sys.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0"
              >
                <div>
                  <p className="font-heading font-bold text-ink">{sys.name}</p>
                  <p className="text-sm text-ink-muted">{sys.custodian_title || '—'}</p>
                  {sys.retention_note && (
                    <p className="text-xs text-ink-muted">{sys.retention_note}</p>
                  )}
                </div>
                <div className="flex flex-wrap gap-2">
                  {sys.urgent_hold && (
                    <Badge tone="red">
                      Short retention — preserve before searching
                    </Badge>
                  )}
                  <Badge tone="amber">Needs verification</Badge>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card>
        {rows.length === 0 ? (
          <EmptyState>No record systems configured.</EmptyState>
        ) : (
          <Table
            columns={[
              {
                header: 'System',
                cell: (r: RecordSystem) => (
                  <div>
                    <p className="font-heading font-bold text-ink">{r.name}</p>
                    <p className="text-xs text-ink-muted">{r.vendor || '—'}</p>
                  </div>
                ),
              },
              { header: 'Category', cell: (r: RecordSystem) => r.category },
              { header: 'Custodian', cell: (r: RecordSystem) => r.custodian_title || '—' },
              {
                header: 'Custodian email',
                cell: (r: RecordSystem) => (
                  canEdit ? (
                    <CustodianEmailCell
                      system={r}
                      onSave={(id, custodian_email) => patchEmail.mutate({ id, custodian_email })}
                    />
                  ) : r.custodian_email || '—'
                ),
              },
              {
                header: 'Default task owner',
                cell: (r: RecordSystem) => r.default_assignee_username || '—',
              },
              { header: 'Retention', cell: (r: RecordSystem) => formatRetention(r) },
              {
                header: 'Urgent hold',
                cell: (r: RecordSystem) =>
                  r.urgent_hold ? <Badge tone="red">Urgent hold</Badge> : '—',
              },
              {
                header: 'Verification',
                cell: (r: RecordSystem) =>
                  r.needs_verification ? (
                    <Badge tone="amber">Needs verification</Badge>
                  ) : (
                    <Badge tone="green">Verified</Badge>
                  ),
              },
              {
                header: 'Status',
                cell: (r: RecordSystem) =>
                  r.active ? <Badge tone="green">Active</Badge> : <Badge>Inactive</Badge>,
              },
            ]}
            rows={rows}
            rowKey={(r) => r.id}
            onRowClick={canEdit ? openEdit : undefined}
          />
        )}
      </Card>

      <Modal open={canEdit && editing !== null} title={editing?.name ?? ''} onClose={closeEdit}>
        {editing && (
          <form onSubmit={submitEdit} className="space-y-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm text-ink-muted">
              <dt className="font-heading font-bold text-ink-muted uppercase text-xs">Category</dt>
              <dd>{editing.category}</dd>
              <dt className="font-heading font-bold text-ink-muted uppercase text-xs">Vendor</dt>
              <dd>{editing.vendor || '—'}</dd>
              <dt className="font-heading font-bold text-ink-muted uppercase text-xs">Integration</dt>
              <dd>{editing.integration || '—'}</dd>
              <dt className="font-heading font-bold text-ink-muted uppercase text-xs">Slug</dt>
              <dd>{editing.slug}</dd>
            </dl>

            <div>
              <span className="font-heading text-sm font-bold text-ink">Triage keywords</span>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {editing.triage_keywords.length > 0 ? (
                  editing.triage_keywords.map((k) => (
                    <Badge key={k} tone="neutral">
                      {k}
                    </Badge>
                  ))
                ) : (
                  <span className="text-sm text-ink-muted">None set.</span>
                )}
              </div>
            </div>

            <div>
              <span className="font-heading text-sm font-bold text-ink">Search checklist</span>
              <pre className="whitespace-pre-wrap mt-1 rounded-md border border-line bg-surface-sunken p-3 text-sm text-ink">
                {editing.search_checklist || 'No checklist recorded.'}
              </pre>
            </div>

            <Field label="Custodian title">
              <Input
                value={editForm.custodian_title}
                onChange={(e) => setEditForm({ ...editForm, custodian_title: e.target.value })}
              />
            </Field>
            <Field label="Custodian email">
              <Input
                type="email"
                value={editForm.custodian_email}
                onChange={(e) => setEditForm({ ...editForm, custodian_email: e.target.value })}
              />
            </Field>
            <Field
              label="Default task assignee"
              hint="New tasks routed to this system are assigned to this staff member."
            >
              <Select
                value={editForm.default_assignee_id}
                onChange={(e) => setEditForm({
                  ...editForm,
                  default_assignee_id: e.target.value,
                })}
                disabled={staffUsers.isPending}
              >
                <option value="">Unassigned</option>
                {assigneeChoices.map((user) => (
                  <option key={user.id} value={user.id}>
                    {user.display_name} ({user.username})
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Retention days" hint="Leave blank if governed by a note instead.">
              <Input
                type="number"
                min={0}
                value={editForm.retention_days}
                onChange={(e) => setEditForm({ ...editForm, retention_days: e.target.value })}
              />
            </Field>
            <Field label="Retention note">
              <Textarea
                rows={2}
                value={editForm.retention_note}
                onChange={(e) => setEditForm({ ...editForm, retention_note: e.target.value })}
              />
            </Field>
            <Field
              label="Change reason"
              hint="Required when changing activation or preservation routing."
            >
              <Textarea
                rows={2}
                minLength={10}
                value={editForm.change_reason}
                onChange={(e) => setEditForm({ ...editForm, change_reason: e.target.value })}
              />
            </Field>
            <div className="flex gap-5">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={editForm.needs_verification}
                  onChange={(e) =>
                    setEditForm({ ...editForm, needs_verification: e.target.checked })
                  }
                />
                Needs verification
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={editForm.active}
                  onChange={(e) => setEditForm({ ...editForm, active: e.target.checked })}
                />
                Active
              </label>
            </div>
            {update.isError && <ErrorNote error={update.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={closeEdit}>
                Cancel
              </Button>
              <Button type="submit" disabled={update.isPending}>
                {update.isPending ? 'Saving…' : 'Save'}
              </Button>
            </div>
          </form>
        )}
      </Modal>

      <Modal open={canEdit && creating} title="Add system" onClose={closeCreate}>
        <form onSubmit={submitCreate} className="space-y-4">
          <Field label="Name">
            <Input
              value={createForm.name}
              onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
              required
            />
          </Field>
          <Field label="Category">
            <Input
              value={createForm.category}
              onChange={(e) => setCreateForm({ ...createForm, category: e.target.value })}
              required
            />
          </Field>
          <Field label="Custodian title">
            <Input
              value={createForm.custodian_title}
              onChange={(e) => setCreateForm({ ...createForm, custodian_title: e.target.value })}
            />
          </Field>
          <Field label="Custodian email">
            <Input
              type="email"
              value={createForm.custodian_email}
              onChange={(e) => setCreateForm({ ...createForm, custodian_email: e.target.value })}
            />
          </Field>
          <Field
            label="Default task assignee"
            hint="Optional. New tasks routed to this system will be assigned automatically."
          >
            <Select
              value={createForm.default_assignee_id}
              onChange={(e) => setCreateForm({
                ...createForm,
                default_assignee_id: e.target.value,
              })}
              disabled={staffUsers.isPending}
            >
              <option value="">Unassigned</option>
              {assigneeChoices.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.display_name} ({user.username})
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Triage keywords" hint="Comma-separated, e.g. bus, video, incident">
            <Input
              value={createForm.keywordsText}
              onChange={(e) => setCreateForm({ ...createForm, keywordsText: e.target.value })}
            />
          </Field>
          <Field
            label="Change reason"
            hint="Explain why this source is being added to legal triage."
          >
            <Textarea
              rows={2}
              minLength={10}
              value={createForm.change_reason}
              onChange={(e) => setCreateForm({ ...createForm, change_reason: e.target.value })}
              required
            />
          </Field>
          {create.isError && <ErrorNote error={create.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeCreate}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </Modal>
    </>
  )
}
