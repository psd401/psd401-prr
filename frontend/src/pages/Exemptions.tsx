// ABOUTME: Exemption taxonomy screen — exemplar list+create+edit vertical the
// ABOUTME: other resource screens follow (query, mutation, modal, toast).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
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
  Spinner,
  Table,
  Textarea,
  useToast,
} from '../ui'

interface Exemption {
  id: number
  citation: string
  short_name: string
  description: string
  requires_second_review: boolean
  active: boolean
}

const emptyForm = {
  citation: '',
  short_name: '',
  description: '',
  requires_second_review: false,
  active: true,
}

export function Exemptions({ roles }: { roles: StaffRole[] }) {
  const qc = useQueryClient()
  const canEdit = roles.includes('records_officer') || roles.includes('admin')
  const toast = useToast()
  const [editing, setEditing] = useState<Exemption | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState(emptyForm)

  const list = useQuery({
    queryKey: ['exemptions'],
    queryFn: () => api<Page<Exemption> | Exemption[]>('/api/staff/exemptions/'),
  })

  const save = useMutation({
    mutationFn: (payload: typeof emptyForm) =>
      editing
        ? api<Exemption>(`/api/staff/exemptions/${editing.id}/`, {
            method: 'PATCH',
            body: payload,
          })
        : api<Exemption>('/api/staff/exemptions/', { method: 'POST', body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['exemptions'] })
      toast('success', editing ? 'Exemption updated.' : 'Exemption added.')
      close()
    },
  })

  function open(row?: Exemption) {
    if (row) {
      setEditing(row)
      setForm({
        citation: row.citation,
        short_name: row.short_name,
        description: row.description,
        requires_second_review: row.requires_second_review,
        active: row.active,
      })
    } else {
      setEditing(null)
      setForm(emptyForm)
      setCreating(true)
    }
  }

  function close() {
    setEditing(null)
    setCreating(false)
    save.reset()
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate(form)
  }

  if (list.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />

  const rows = unwrap(list.data)

  return (
    <>
      <PageHeader
        title="Exemptions"
        subtitle="RCW citation taxonomy — every redaction or withholding must cite one. Counsel review required before production use."
        actions={canEdit ? <Button onClick={() => open()}>Add exemption</Button> : undefined}
      />
      <Card>
        {rows.length === 0 ? (
          <EmptyState>No exemptions configured.</EmptyState>
        ) : (
          <Table
            columns={[
              { header: 'Citation', cell: (r: Exemption) => <span className="font-heading font-bold">{r.citation}</span> },
              { header: 'Short name', cell: (r: Exemption) => r.short_name },
              {
                header: 'Description',
                cell: (r: Exemption) => r.description || '—',
              },
              {
                header: 'Second review',
                cell: (r: Exemption) =>
                  r.requires_second_review ? <Badge tone="amber">Required</Badge> : '—',
              },
              {
                header: 'Status',
                cell: (r: Exemption) =>
                  r.active ? <Badge tone="green">Active</Badge> : <Badge>Inactive</Badge>,
              },
            ]}
            rows={rows}
            rowKey={(r) => r.id}
            onRowClick={canEdit ? open : undefined}
          />
        )}
      </Card>

      <Modal
        open={canEdit && (creating || editing !== null)}
        title={editing ? `Edit ${editing.citation}` : 'Add exemption'}
        onClose={close}
      >
        <form onSubmit={submit} className="space-y-4">
          <Field label="Citation" hint='e.g. "RCW 42.56.230(1)"'>
            <Input
              value={form.citation}
              onChange={(e) => setForm({ ...form, citation: e.target.value })}
              required
            />
          </Field>
          <Field label="Short name">
            <Input
              value={form.short_name}
              onChange={(e) => setForm({ ...form, short_name: e.target.value })}
              required
            />
          </Field>
          <Field label="Description">
            <Textarea
              rows={3}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </Field>
          <div className="flex gap-5">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.requires_second_review}
                onChange={(e) =>
                  setForm({ ...form, requires_second_review: e.target.checked })
                }
              />
              Requires second review
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.active}
                onChange={(e) => setForm({ ...form, active: e.target.checked })}
              />
              Active
            </label>
          </div>
          {save.isError && <ErrorNote error={save.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </Modal>
    </>
  )
}
