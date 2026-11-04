// ABOUTME: Audit log screen — append-only oversight record of every staff
// ABOUTME: action; filterable, read-only, row click reveals full entry JSON.
import { useInfiniteQuery } from '@tanstack/react-query'
import { useEffect, useState, type ChangeEvent } from 'react'
import { api, unwrap, type Page } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import { Button, Card, EmptyState, ErrorNote, Field, Input, Modal, Spinner, Table } from '../ui'

interface AuditLog {
  id: number
  at: string
  actor: number | null
  actor_username: string | null
  action: string
  target: string
  detail: unknown
  ip_address: string | null
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function Audit() {
  const [actionInput, setActionInput] = useState('')
  const [targetInput, setTargetInput] = useState('')
  const [since, setSince] = useState('')
  const [filters, setFilters] = useState({ action: '', target: '' })
  const [selected, setSelected] = useState<AuditLog | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters({ action: actionInput, target: targetInput })
    }, 350)
    return () => clearTimeout(timer)
  }, [actionInput, targetInput])

  function handleSinceChange(e: ChangeEvent<HTMLInputElement>) {
    setSince(e.target.value)
  }

  const params = new URLSearchParams()
  if (filters.action) params.set('action', filters.action)
  if (filters.target) params.set('target', filters.target)
  if (since) params.set('since', since)
  const qs = params.toString()
  const initialPage = `/api/staff/audit-log/${qs ? `?${qs}` : ''}`

  const list = useInfiniteQuery({
    queryKey: ['audit-log', filters.action, filters.target, since],
    initialPageParam: initialPage,
    queryFn: ({ pageParam }) =>
      api<Page<AuditLog> | AuditLog[]>(pageParam, { fetchAllPages: false }),
    getNextPageParam: (lastPage) =>
      Array.isArray(lastPage) ? undefined : (lastPage.next ?? undefined),
  })

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle="Append-only record of every action — nothing here can be edited or deleted."
      />

      <Card className="mb-5">
        <div className="flex flex-wrap gap-4">
          <div className="w-56">
            <Field label="Action">
              <Input
                placeholder="e.g. request.created"
                value={actionInput}
                onChange={(e) => setActionInput(e.target.value)}
              />
            </Field>
          </div>
          <div className="w-56">
            <Field label="Target">
              <Input
                placeholder="e.g. PRR-2026-0042"
                value={targetInput}
                onChange={(e) => setTargetInput(e.target.value)}
              />
            </Field>
          </div>
          <div className="w-48">
            <Field label="Since">
              <Input type="date" value={since} onChange={handleSinceChange} />
            </Field>
          </div>
        </div>
      </Card>

      {list.isPending && <Spinner />}
      {list.isError && !list.data && <ErrorNote error={list.error} />}

      {list.data && (
        <Card>
          {(() => {
            const pages = list.data.pages
            const rows = pages.flatMap((page) => unwrap(page))
            const firstPage = pages[0]
            const total = firstPage && !Array.isArray(firstPage) ? firstPage.count : rows.length
            return (
              <>
                {rows.length === 0 ? (
                  <EmptyState>No audit entries match the current filters.</EmptyState>
                ) : (
                  <Table
                    columns={[
                      { header: 'Timestamp', cell: (r: AuditLog) => formatTimestamp(r.at) },
                      {
                        header: 'Action',
                        cell: (r: AuditLog) => (
                          <span className="font-heading font-bold">{r.action}</span>
                        ),
                      },
                      { header: 'Target', cell: (r: AuditLog) => r.target || '—' },
                      {
                        header: 'Actor',
                        cell: (r: AuditLog) =>
                          r.actor_username ?? (r.actor === null ? 'System' : 'Staff user'),
                      },
                      { header: 'IP', cell: (r: AuditLog) => r.ip_address ?? '—' },
                    ]}
                    rows={rows}
                    rowKey={(r) => r.id}
                    onRowClick={setSelected}
                  />
                )}
                {rows.length > 0 && (
                  <p className="mt-4 text-sm text-ink-muted">
                    Showing {rows.length} of {total} total entries
                  </p>
                )}
                {list.hasNextPage && (
                  <div className="mt-4">
                    <Button
                      variant="ghost"
                      disabled={list.isFetchingNextPage}
                      onClick={() => void list.fetchNextPage()}
                    >
                      {list.isFetchingNextPage ? 'Loading more…' : 'Load more'}
                    </Button>
                  </div>
                )}
                {list.isFetchNextPageError && <ErrorNote error={list.error} />}
              </>
            )
          })()}
        </Card>
      )}

      <Modal
        open={selected !== null}
        title={selected ? `Audit entry #${selected.id}` : 'Audit entry'}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <pre className="max-h-96 overflow-auto rounded-md border border-line bg-surface-sunken p-3 text-xs text-ink">
            {JSON.stringify(selected, null, 2)}
          </pre>
        )}
      </Modal>
    </>
  )
}
