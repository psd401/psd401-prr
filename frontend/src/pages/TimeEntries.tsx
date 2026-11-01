// ABOUTME: Time entries screen — staff log hours spent per request; full
// ABOUTME: create/delete vertical with confirm-gated deletes and toasts.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import type { StaffRole } from '../api/auth'
import { Button, Card, EmptyState, ErrorNote, Field, Input, Modal, Spinner, Table, Textarea, useToast } from '../ui'

interface TimeEntry {
  id: number
  request: number
  request_number: string
  staff_username: string
  date: string
  minutes: number
  activity: string
}

interface RequestChoice {
  id: string
  number: string
}

const emptyForm = {
  request: '',
  date: '',
  minutes: '',
  activity: '',
}

function formatHours(minutes: number): string {
  return `${(minutes / 60).toFixed(2)} hrs`
}

export function TimeEntries({
  roles,
  requestNumber,
}: {
  roles: StaffRole[]
  requestNumber?: string
}) {
  const qc = useQueryClient()
  const [searchParams] = useSearchParams()
  const canDelete = roles.includes('records_officer')
  const toast = useToast()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState(emptyForm)
  const [deleting, setDeleting] = useState<TimeEntry | null>(null)

  const list = useQuery({
    queryKey: ['time-entries', requestNumber ?? 'all'],
    queryFn: () => api<Page<TimeEntry> | TimeEntry[]>(
      requestNumber
        ? `/api/staff/time-entries/?request=${encodeURIComponent(requestNumber)}`
        : '/api/staff/time-entries/',
    ),
  })
  const requests = useQuery({
    queryKey: ['requests', 'time-entry-choices'],
    queryFn: () => api<Page<RequestChoice> | RequestChoice[]>('/api/staff/requests/'),
    enabled: creating && !requestNumber,
  })
  const scopedRequest = useQuery({
    queryKey: ['request', requestNumber, 'time-entry-choice'],
    queryFn: () => api<RequestChoice>(`/api/staff/requests/${requestNumber}/`),
    enabled: creating && Boolean(requestNumber),
  })

  const create = useMutation({
    mutationFn: (payload: typeof emptyForm) =>
      api<TimeEntry>('/api/staff/time-entries/', {
        method: 'POST',
        body: {
          request: requestNumber ? scopedRequest.data?.id : payload.request,
          date: payload.date,
          minutes: Number(payload.minutes),
          activity: payload.activity,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['time-entries'] })
      toast('success', 'Time logged.')
      closeCreate()
    },
    onError: (err) => toast('error', err instanceof Error ? err.message : 'Log failed.'),
  })

  const del = useMutation({
    mutationFn: (id: number) => api<void>(`/api/staff/time-entries/${id}/`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['time-entries'] })
      toast('success', 'Time entry deleted.')
      setDeleting(null)
    },
    onError: (err) => toast('error', err instanceof Error ? err.message : 'Delete failed.'),
  })

  function openCreate() {
    setForm(emptyForm)
    setCreating(true)
  }

  function closeCreate() {
    setCreating(false)
    create.reset()
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    if (requestNumber && !scopedRequest.data) return
    create.mutate(form)
  }

  useEffect(() => {
    if (list.isSuccess && searchParams.get('section') === 'time-entries') {
      headingRef.current?.focus()
    }
  }, [list.isSuccess, searchParams])

  if (list.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />

  const rows = unwrap(list.data)

  return (
    <>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3
            ref={headingRef}
            id="case-time-entries"
            tabIndex={-1}
            className="font-heading text-xl font-bold text-pacific outline-none"
          >
            Time entries
          </h3>
          <p className="mt-1 text-sm text-pacific/80">
            Staff hours logged against this case.
          </p>
        </div>
        <Button onClick={openCreate}>Log time</Button>
      </div>

      <Card>
        {rows.length === 0 ? (
          <EmptyState>No time logged yet.</EmptyState>
        ) : (
          <Table
            columns={[
              {
                header: 'Request',
                cell: (r: TimeEntry) => <span className="font-heading font-bold">{r.request_number}</span>,
              },
              { header: 'Staff', cell: (r: TimeEntry) => r.staff_username },
              { header: 'Hours', cell: (r: TimeEntry) => formatHours(r.minutes), align: 'right' },
              { header: 'Date', cell: (r: TimeEntry) => r.date },
              { header: 'Notes', cell: (r: TimeEntry) => r.activity || '—' },
              {
                header: '',
                align: 'right',
                cell: (r: TimeEntry) => canDelete ? (
                  <Button
                    variant="ghost"
                    onClick={(e) => {
                      e.stopPropagation()
                      setDeleting(r)
                    }}
                  >
                    Delete
                  </Button>
                ) : null,
              },
            ]}
            rows={rows}
            rowKey={(r) => r.id}
          />
        )}
      </Card>

      <Modal open={creating} title="Log time" onClose={closeCreate}>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Case">
            {requestNumber ? (
              <p className="rounded-md border border-driftwood/60 bg-skylight px-3 py-2 text-sm font-semibold text-pacific">
                {requestNumber}
              </p>
            ) : (
              <select
                className="w-full rounded-md border border-driftwood bg-white px-3 py-2 text-sm text-pacific"
                value={form.request}
                onChange={(e) => setForm({ ...form, request: e.target.value })}
                disabled={requests.isPending || requests.isError}
                required
              >
                <option value="">Select a request</option>
                {requests.data && unwrap(requests.data).map((request) => (
                  <option key={request.id} value={request.id}>{request.number}</option>
                ))}
              </select>
            )}
          </Field>
          {!requestNumber && requests.isPending && <Spinner label="Loading requests…" />}
          {!requestNumber && requests.isError && <ErrorNote error={requests.error} />}
          {requestNumber && scopedRequest.isPending && <Spinner label="Loading case…" />}
          {requestNumber && scopedRequest.isError && <ErrorNote error={scopedRequest.error} />}
          <Field label="Date">
            <Input
              type="date"
              value={form.date}
              onChange={(e) => setForm({ ...form, date: e.target.value })}
              required
            />
          </Field>
          <Field label="Minutes">
            <Input
              type="number"
              min={1}
              value={form.minutes}
              onChange={(e) => setForm({ ...form, minutes: e.target.value })}
              required
            />
          </Field>
          <Field label="Notes">
            <Textarea
              rows={3}
              value={form.activity}
              onChange={(e) => setForm({ ...form, activity: e.target.value })}
            />
          </Field>
          {create.isError && <ErrorNote error={create.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeCreate}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={create.isPending || Boolean(requestNumber && !scopedRequest.data)}
            >
              {create.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal open={canDelete && deleting !== null} title="Delete time entry" onClose={() => setDeleting(null)}>
        <div className="space-y-4">
          <p className="text-sm text-pacific">
            Delete this {deleting ? formatHours(deleting.minutes) : ''} entry for{' '}
            <span className="font-heading font-bold">{deleting?.request_number}</span> on{' '}
            {deleting?.date}? This cannot be undone.
          </p>
          {del.isError && <ErrorNote error={del.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="danger"
              disabled={del.isPending}
              onClick={() => deleting && del.mutate(deleting.id)}
            >
              {del.isPending ? 'Deleting…' : 'Delete'}
            </Button>
          </div>
        </div>
      </Modal>
    </>
  )
}
