// ABOUTME: Records Officer screen for the district knowledge pack the intent
// ABOUTME: advisory reads: role accounts, noise senders, and system search notes.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { api, unwrap, type Page } from '../api/client'
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

interface RoleAccount {
  id: number
  role: string
  account_email: string
  starts_on: string | null
  ends_on: string | null
  note: string
}

interface NoiseSender {
  id: number
  kind: 'domain' | 'address' | 'list'
  value: string
  note: string
  active: boolean
}

interface SystemNote {
  id: number
  record_system_id: number
  record_system_slug: string
  record_system_name: string
  notes: string
}

interface RecordSystemOption {
  id: number
  name: string
  active: boolean
}

// Reads sit together, away from the writes below, so each list call is plainly
// a GET to the capability parity scan.
const fetchRoleAccounts = () => api<Page<RoleAccount> | RoleAccount[]>('/api/staff/knowledge/role-accounts/')
const fetchNoiseSenders = () => api<Page<NoiseSender> | NoiseSender[]>('/api/staff/knowledge/noise-senders/')
const fetchSystemNotes = () => api<Page<SystemNote> | SystemNote[]>('/api/staff/knowledge/system-notes/')
const fetchRecordSystems = () => api<Page<RecordSystemOption> | RecordSystemOption[]>('/api/staff/record-systems/')

const NOISE_KIND_LABELS: Record<NoiseSender['kind'], string> = {
  domain: 'Sender domain',
  address: 'Sender address',
  list: 'Mailing list',
}

/**
 * The save/remove plumbing every knowledge section shares. Each write is
 * audited server-side; a failure is toasted as well as shown in the dialog,
 * so nobody who looked away reads silence as success.
 */
function useSection<T extends { id: number }>(queryKey: string, noun: string) {
  const qc = useQueryClient()
  const toast = useToast()
  const [editing, setEditing] = useState<T | null>(null)
  const [creating, setCreating] = useState(false)
  const [removing, setRemoving] = useState<T | null>(null)
  const done = (message: string) => {
    qc.invalidateQueries({ queryKey: [queryKey] })
    toast('success', message)
  }
  return {
    editing,
    creating,
    removing,
    setRemoving,
    openCreate: () => {
      setEditing(null)
      setCreating(true)
    },
    openEdit: (row: T) => {
      setCreating(false)
      setEditing(row)
    },
    close: () => {
      setEditing(null)
      setCreating(false)
    },
    onSaved: (wasEditing: boolean) => {
      done(wasEditing ? `${noun} updated.` : `${noun} added.`)
      setEditing(null)
      setCreating(false)
    },
    onRemoved: () => {
      done(`${noun} removed.`)
      setRemoving(null)
    },
    onError: (error: Error) => toast('error', error.message),
  }
}

function FormActions({ pending, onCancel }: { pending: boolean; onCancel: () => void }) {
  return (
    <div className="flex justify-end gap-2">
      <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
      <Button type="submit" disabled={pending}>{pending ? 'Saving…' : 'Save'}</Button>
    </div>
  )
}

function RemoveDialog({
  title,
  pending,
  onCancel,
  onConfirm,
  children,
}: {
  title: string
  pending: boolean
  onCancel: () => void
  onConfirm: () => void
  children: ReactNode
}) {
  return (
    <Modal open={title !== ''} title={title} onClose={onCancel}>
      <div className="space-y-4">
        <p className="text-sm text-pacific">{children}</p>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
          <Button type="button" disabled={pending} onClick={onConfirm}>
            {pending ? 'Removing…' : 'Remove'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

function RemoveButton({ onClick }: { onClick: () => void }) {
  return (
    <Button
      variant="ghost"
      onClick={(event) => {
        event.stopPropagation()
        onClick()
      }}
    >
      Remove
    </Button>
  )
}

const emptyRoleForm = { role: '', account_email: '', starts_on: '', ends_on: '', note: '' }

function RoleAccounts() {
  const section = useSection<RoleAccount>('knowledge-role-accounts', 'Role account')
  const [form, setForm] = useState(emptyRoleForm)
  const list = useQuery({
    queryKey: ['knowledge-role-accounts'],
    queryFn: fetchRoleAccounts,
  })
  const save = useMutation({
    mutationFn: (payload: typeof emptyRoleForm) => {
      const body = { ...payload, starts_on: payload.starts_on || null, ends_on: payload.ends_on || null }
      return section.editing
        ? api<RoleAccount>(`/api/staff/knowledge/role-accounts/${section.editing.id}/`, { method: 'PATCH', body })
        : api<RoleAccount>('/api/staff/knowledge/role-accounts/', { method: 'POST', body })
    },
    onSuccess: () => section.onSaved(section.editing !== null),
    onError: section.onError,
  })
  const remove = useMutation({
    mutationFn: (row: RoleAccount) => api<void>(`/api/staff/knowledge/role-accounts/${row.id}/`, { method: 'DELETE' }),
    onSuccess: section.onRemoved,
    onError: section.onError,
  })

  function open(row?: RoleAccount) {
    save.reset()
    if (row) {
      setForm({
        role: row.role,
        account_email: row.account_email,
        starts_on: row.starts_on ?? '',
        ends_on: row.ends_on ?? '',
        note: row.note,
      })
      section.openEdit(row)
    } else {
      setForm(emptyRoleForm)
      section.openCreate()
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate(form)
  }

  const rows = list.data ? unwrap(list.data) : []
  return (
    <Card title="Role accounts" actions={<Button onClick={() => open()}>Add role account</Button>}>
      <p className="mb-4 text-sm text-pacific/80">
        Which district account held a role, and when. A request that names
        &ldquo;the principal&rdquo; is scoped to whoever held that role during
        the dates the request reaches. Leave a date blank for an open end.
      </p>
      {list.isPending && <Spinner />}
      {list.isError && <ErrorNote error={list.error} />}
      {list.isSuccess && (rows.length === 0 ? (
        <EmptyState>No role accounts yet.</EmptyState>
      ) : (
        <Table
          label="Role accounts"
          columns={[
            { header: 'Role', cell: (row: RoleAccount) => <span className="font-semibold">{row.role}</span> },
            { header: 'Account', cell: (row: RoleAccount) => row.account_email },
            { header: 'From', cell: (row: RoleAccount) => row.starts_on ?? 'Open' },
            { header: 'Until', cell: (row: RoleAccount) => row.ends_on ?? 'Open' },
            { header: 'Note', cell: (row: RoleAccount) => row.note || '—' },
            { header: 'Remove', cell: (row: RoleAccount) => <RemoveButton onClick={() => section.setRemoving(row)} /> },
          ]}
          rows={rows}
          rowKey={(row) => row.id}
          onRowClick={open}
        />
      ))}
      <Modal
        open={section.creating || section.editing !== null}
        title={section.editing ? `Edit ${section.editing.role}` : 'Add role account'}
        onClose={section.close}
      >
        <form onSubmit={submit} className="space-y-4">
          <Field label="Role" hint="For example, a school's principal or the HR director.">
            <Input value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} required />
          </Field>
          <Field label="Account email">
            <Input
              type="email"
              value={form.account_email}
              onChange={(e) => setForm({ ...form, account_email: e.target.value })}
              required
            />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="From">
              <Input type="date" value={form.starts_on} onChange={(e) => setForm({ ...form, starts_on: e.target.value })} />
            </Field>
            <Field label="Until">
              <Input type="date" value={form.ends_on} onChange={(e) => setForm({ ...form, ends_on: e.target.value })} />
            </Field>
          </div>
          <Field label="Note">
            <Input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          </Field>
          {save.isError && <ErrorNote error={save.error} />}
          <FormActions pending={save.isPending} onCancel={section.close} />
        </form>
      </Modal>
      <RemoveDialog
        title={section.removing ? `Remove ${section.removing.role}` : ''}
        pending={remove.isPending}
        onCancel={() => section.setRemoving(null)}
        onConfirm={() => section.removing && remove.mutate(section.removing)}
      >
        The advisory stops seeing this role&rsquo;s account. Nothing already
        collected or proposed changes.
      </RemoveDialog>
    </Card>
  )
}

const emptyNoiseForm = { kind: 'domain' as NoiseSender['kind'], value: '', note: '', active: true }

function NoiseSenders() {
  const section = useSection<NoiseSender>('knowledge-noise-senders', 'Noise sender')
  const [form, setForm] = useState(emptyNoiseForm)
  const list = useQuery({
    queryKey: ['knowledge-noise-senders'],
    queryFn: fetchNoiseSenders,
  })
  const save = useMutation({
    mutationFn: (body: typeof emptyNoiseForm) =>
      section.editing
        ? api<NoiseSender>(`/api/staff/knowledge/noise-senders/${section.editing.id}/`, { method: 'PATCH', body })
        : api<NoiseSender>('/api/staff/knowledge/noise-senders/', { method: 'POST', body }),
    onSuccess: () => section.onSaved(section.editing !== null),
    onError: section.onError,
  })
  const remove = useMutation({
    mutationFn: (row: NoiseSender) => api<void>(`/api/staff/knowledge/noise-senders/${row.id}/`, { method: 'DELETE' }),
    onSuccess: section.onRemoved,
    onError: section.onError,
  })

  function open(row?: NoiseSender) {
    save.reset()
    if (row) {
      setForm({ kind: row.kind, value: row.value, note: row.note, active: row.active })
      section.openEdit(row)
    } else {
      setForm(emptyNoiseForm)
      section.openCreate()
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate(form)
  }

  const rows = list.data ? unwrap(list.data) : []
  return (
    <Card title="Noise senders" actions={<Button onClick={() => open()}>Add noise sender</Button>}>
      <p className="mb-4 text-sm text-pacific/80">
        District mass mail, newsletters, and notifications a responsive search
        almost never wants. The advisory is told not to search for them. An
        entry excludes nothing on its own: a person still reviews every count.
      </p>
      {list.isPending && <Spinner />}
      {list.isError && <ErrorNote error={list.error} />}
      {list.isSuccess && (rows.length === 0 ? (
        <EmptyState>No noise senders yet.</EmptyState>
      ) : (
        <Table
          label="Noise senders"
          columns={[
            { header: 'Kind', cell: (row: NoiseSender) => NOISE_KIND_LABELS[row.kind] },
            { header: 'Sender', cell: (row: NoiseSender) => <span className="font-semibold">{row.value}</span> },
            { header: 'Note', cell: (row: NoiseSender) => row.note || '—' },
            {
              header: 'Status',
              cell: (row: NoiseSender) => (row.active ? <Badge tone="green">Active</Badge> : <Badge>Inactive</Badge>),
            },
            { header: 'Remove', cell: (row: NoiseSender) => <RemoveButton onClick={() => section.setRemoving(row)} /> },
          ]}
          rows={rows}
          rowKey={(row) => row.id}
          onRowClick={open}
        />
      ))}
      <Modal
        open={section.creating || section.editing !== null}
        title={section.editing ? `Edit ${section.editing.value}` : 'Add noise sender'}
        onClose={section.close}
      >
        <form onSubmit={submit} className="space-y-4">
          <Field label="Kind">
            <Select
              className="w-full"
              value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value as NoiseSender['kind'] })}
            >
              {Object.entries(NOISE_KIND_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </Select>
          </Field>
          <Field label="Sender" hint="A domain (news.example.org), an address, or a list's address or list ID.">
            <Input value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} required />
          </Field>
          <Field label="Note">
            <Input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.active}
              onChange={(e) => setForm({ ...form, active: e.target.checked })}
            />
            Active
          </label>
          {save.isError && <ErrorNote error={save.error} />}
          <FormActions pending={save.isPending} onCancel={section.close} />
        </form>
      </Modal>
      <RemoveDialog
        title={section.removing ? `Remove ${section.removing.value}` : ''}
        pending={remove.isPending}
        onCancel={() => section.setRemoving(null)}
        onConfirm={() => section.removing && remove.mutate(section.removing)}
      >
        The advisory stops treating this sender as noise. Deactivate instead
        to keep the entry on record.
      </RemoveDialog>
    </Card>
  )
}

const emptyNoteForm = { record_system_id: '', notes: '' }

function SystemNotes() {
  const section = useSection<SystemNote>('knowledge-system-notes', 'Search notes')
  const [form, setForm] = useState(emptyNoteForm)
  const list = useQuery({
    queryKey: ['knowledge-system-notes'],
    queryFn: fetchSystemNotes,
  })
  const systems = useQuery({
    queryKey: ['record-systems', 'knowledge'],
    queryFn: fetchRecordSystems,
  })
  const save = useMutation({
    mutationFn: (payload: typeof emptyNoteForm) =>
      section.editing
        ? api<SystemNote>(`/api/staff/knowledge/system-notes/${section.editing.id}/`, { method: 'PATCH', body: { notes: payload.notes } })
        : api<SystemNote>('/api/staff/knowledge/system-notes/', { method: 'POST', body: { record_system_id: Number(payload.record_system_id), notes: payload.notes } }),
    onSuccess: () => section.onSaved(section.editing !== null),
    onError: section.onError,
  })
  const remove = useMutation({
    mutationFn: (row: SystemNote) => api<void>(`/api/staff/knowledge/system-notes/${row.id}/`, { method: 'DELETE' }),
    onSuccess: section.onRemoved,
    onError: section.onError,
  })

  const rows = list.data ? unwrap(list.data) : []
  const noted = new Set(rows.map((row) => row.record_system_id))
  const available = (systems.data ? unwrap(systems.data) : [])
    .filter((system) => system.active && !noted.has(system.id))

  function open(row?: SystemNote) {
    save.reset()
    if (row) {
      setForm({ record_system_id: String(row.record_system_id), notes: row.notes })
      section.openEdit(row)
    } else {
      setForm({ ...emptyNoteForm, record_system_id: available[0] ? String(available[0].id) : '' })
      section.openCreate()
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate(form)
  }

  return (
    <Card
      title="System search notes"
      actions={<Button onClick={() => open()} disabled={available.length === 0}>Add search notes</Button>}
    >
      <p className="mb-4 text-sm text-pacific/80">
        How each registry system is actually searched, in your own words. The
        advisory reads these beside the system&rsquo;s custodian, retention,
        and checklist. Re-seeding the registry never changes them.
      </p>
      {list.isPending && <Spinner />}
      {list.isError && <ErrorNote error={list.error} />}
      {systems.isError && <ErrorNote error={systems.error} />}
      {list.isSuccess && (rows.length === 0 ? (
        <EmptyState>No system search notes yet.</EmptyState>
      ) : (
        <Table
          label="System search notes"
          columns={[
            { header: 'System', cell: (row: SystemNote) => <span className="font-semibold">{row.record_system_name}</span> },
            { header: 'Search notes', grow: true, cell: (row: SystemNote) => <span className="whitespace-pre-line">{row.notes}</span> },
            { header: 'Remove', cell: (row: SystemNote) => <RemoveButton onClick={() => section.setRemoving(row)} /> },
          ]}
          rows={rows}
          rowKey={(row) => row.id}
          onRowClick={open}
        />
      ))}
      <Modal
        open={section.creating || section.editing !== null}
        title={section.editing ? `Edit notes for ${section.editing.record_system_name}` : 'Add search notes'}
        onClose={section.close}
      >
        <form onSubmit={submit} className="space-y-4">
          {!section.editing && (
            <Field label="System">
              <Select
                className="w-full"
                value={form.record_system_id}
                onChange={(e) => setForm({ ...form, record_system_id: e.target.value })}
                required
              >
                {available.map((system) => (
                  <option key={system.id} value={system.id}>{system.name}</option>
                ))}
              </Select>
            </Field>
          )}
          <Field label="Search notes">
            <Textarea rows={5} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} required />
          </Field>
          {save.isError && <ErrorNote error={save.error} />}
          <FormActions pending={save.isPending} onCancel={section.close} />
        </form>
      </Modal>
      <RemoveDialog
        title={section.removing ? `Remove notes for ${section.removing.record_system_name}` : ''}
        pending={remove.isPending}
        onCancel={() => section.setRemoving(null)}
        onConfirm={() => section.removing && remove.mutate(section.removing)}
      >
        The advisory stops seeing these notes. The registry entry itself does
        not change.
      </RemoveDialog>
    </Card>
  )
}

export function Knowledge() {
  return (
    <>
      <PageHeader
        title="Knowledge"
        subtitle={
          'What you know about where district records live, for the request '
          + 'intent advisory to scope searches by. Every change is audited. '
          + 'Nothing here routes a request, holds, collects, or reaches a '
          + 'requester; it is context for a proposal a person still reviews.'
        }
      />
      <div className="space-y-6">
        <RoleAccounts />
        <NoiseSenders />
        <SystemNotes />
      </div>
    </>
  )
}
