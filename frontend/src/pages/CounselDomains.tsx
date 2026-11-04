// ABOUTME: Application-admin screen for the district's own counsel email
// ABOUTME: domains, which separate records into the privilege queue for review.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
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
  Spinner,
  Table,
  useToast,
} from '../ui'

interface CounselDomain {
  id: number
  domain: string
  note: string
  active: boolean
  created_at: string
  updated_at: string
}

const emptyForm = { domain: '', note: '', active: true }

export function CounselDomains() {
  const qc = useQueryClient()
  const toast = useToast()
  const [editing, setEditing] = useState<CounselDomain | null>(null)
  const [creating, setCreating] = useState(false)
  const [removing, setRemoving] = useState<CounselDomain | null>(null)
  const [form, setForm] = useState(emptyForm)

  const list = useQuery({
    queryKey: ['counsel-domains'],
    queryFn: () => api<Page<CounselDomain> | CounselDomain[]>(
      '/api/staff/counsel-domains/',
    ),
  })

  const save = useMutation({
    mutationFn: (payload: typeof emptyForm) =>
      editing
        ? api<CounselDomain>(`/api/staff/counsel-domains/${editing.id}/`, {
          method: 'PATCH',
          body: payload,
        })
        : api<CounselDomain>('/api/staff/counsel-domains/', {
          method: 'POST',
          body: payload,
        }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['counsel-domains'] })
      toast('success', editing ? 'Counsel domain updated.' : 'Counsel domain added.')
      close()
    },
    // The modal shows an ErrorNote, but a failure here is announced the same
    // way every other failure on this page is. Silence would read as success
    // to anyone who had already looked away from the dialog.
    onError: (error) => toast('error', error.message),
  })

  const remove = useMutation({
    mutationFn: (row: CounselDomain) => api<void>(
      `/api/staff/counsel-domains/${row.id}/`,
      { method: 'DELETE' },
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['counsel-domains'] })
      toast('success', 'Counsel domain removed.')
      setRemoving(null)
    },
    onError: (error) => toast('error', error.message),
  })

  function open(row?: CounselDomain) {
    if (row) {
      setEditing(row)
      setForm({ domain: row.domain, note: row.note, active: row.active })
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

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate(form)
  }

  if (list.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />

  const rows = unwrap(list.data)

  return (
    <>
      <PageHeader
        title="District counsel domains"
        subtitle={
          'Email domains belonging to the district’s own legal counsel. '
          + 'Records touching these domains are tagged into the privilege queue '
          + 'so a reviewer reads them before anything ships. An entry withholds '
          + 'nothing: full withholding still needs a named reviewer, an '
          + 'exemption citation, and second review.'
        }
        actions={<Button onClick={() => open()}>Add domain</Button>}
      />
      <Card>
        {rows.length === 0 ? (
          <EmptyState>
            No counsel domains are configured, so nothing is tagged as district
            counsel. A plaintiff&rsquo;s firm needs no entry here — the external
            sender tag and the sender domain filter already separate it.
          </EmptyState>
        ) : (
          <Table
            columns={[
              {
                header: 'Domain',
                cell: (row: CounselDomain) => (
                  <span className="font-heading font-bold">{row.domain}</span>
                ),
              },
              { header: 'Note', cell: (row: CounselDomain) => row.note || '—' },
              {
                header: 'Status',
                cell: (row: CounselDomain) => (
                  row.active
                    ? <Badge tone="green">Active</Badge>
                    : <Badge>Inactive</Badge>
                ),
              },
              {
                header: 'Remove',
                cell: (row: CounselDomain) => (
                  <Button
                    variant="ghost"
                    onClick={(event) => {
                      event.stopPropagation()
                      setRemoving(row)
                    }}
                  >
                    Remove
                  </Button>
                ),
              },
            ]}
            rows={rows}
            rowKey={(row) => row.id}
            onRowClick={open}
          />
        )}
      </Card>

      <Modal
        open={creating || editing !== null}
        title={editing ? `Edit ${editing.domain}` : 'Add counsel domain'}
        onClose={close}
      >
        <form onSubmit={submit} className="space-y-4">
          <Field label="Domain" hint='One exact email domain, e.g. "lawfirm.com"'>
            <Input
              value={form.domain}
              onChange={(event) => setForm({ ...form, domain: event.target.value })}
              required
            />
          </Field>
          <Field label="Note" hint="Which firm this is, for a later access review.">
            <Input
              value={form.note}
              onChange={(event) => setForm({ ...form, note: event.target.value })}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.active}
              onChange={(event) => setForm({ ...form, active: event.target.checked })}
            />
            Active
          </label>
          {save.isError && <ErrorNote error={save.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={close}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={removing !== null}
        title={removing ? `Remove ${removing.domain}` : ''}
        onClose={() => setRemoving(null)}
      >
        <div className="space-y-4">
          <p className="text-sm text-ink">
            Records touching this domain stop carrying the district counsel tag.
            Nothing already reviewed, withheld, or released changes. Deactivate
            instead if you want to keep the entry on record.
          </p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={remove.isPending}
              onClick={() => removing && remove.mutate(removing)}
            >
              {remove.isPending ? 'Removing…' : 'Remove'}
            </Button>
          </div>
        </div>
      </Modal>
    </>
  )
}
