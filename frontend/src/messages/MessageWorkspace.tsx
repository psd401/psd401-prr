// ABOUTME: Scalable request message browser with bounded summaries, one mounted
// ABOUTME: detail, explicit pagination, persistent filters, and UX-01 work state.
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import {
  useEffect,
  useCallback,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react'
import { useSearchParams } from 'react-router'
import { NoActionModal } from '../NoActionModal'
import { useMe, type StaffRole } from '../api/auth'
import { api, unwrap, type Page } from '../api/client'
import {
  CORRESPONDENCE_KIND_LABELS,
  CORRESPONDENCE_STATUS_LABELS,
  DELIVERY_STATUS_LABELS,
  correspondenceDetailQueryKey,
  correspondenceStatusTone,
  type CorrespondenceDetail,
  type CorrespondenceSummary,
  type CorrespondenceThreadSummary,
  type MessageWorkItem,
} from '../correspondence'
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
  useToast,
} from '../ui'
import {
  invalidateBoundedInfiniteQueries,
  markBoundedInfiniteQueriesStale,
} from './boundedInfiniteCache'

const FILTER_KEYS = [
  'direction',
  'kind',
  'status',
  'work_state',
  'read_state',
  'delivery_state',
  'matching_state',
  'has_attachments',
  'date_from',
  'date_to',
  'assignee',
] as const

type FilterKey = typeof FILTER_KEYS[number]
type WorkAction = 'read' | 'unread' | 'waiting' | 'handled' | 'reopen'

interface StaffUserChoice {
  id: number
  username: string
  display_name: string
}

const WORK_DISPOSITION_LABELS: Record<MessageWorkItem['disposition'], string> = {
  needs_attention: 'Needs attention',
  waiting: 'Waiting',
  handled: 'Handled',
  no_action_required: 'No action required',
  source_excluded: 'Source excluded',
}

const KINDS = Object.entries(CORRESPONDENCE_KIND_LABELS)

function messageParam(key: string) {
  return `message_${key}`
}

function idempotencyKey(action: string) {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `message-workspace:${action}:${random}`
}

function pageRows<T>(pages: Array<Page<T> | T[]> | undefined): T[] {
  return pages?.flatMap((page) => unwrap(page)) ?? []
}

function pageNext<T>(page: Page<T> | T[]) {
  if (Array.isArray(page) || !page.next) return undefined
  // DRF may build an absolute link from the upstream Django host. Keep
  // browser pagination on the current SPA origin (including Vite's proxy).
  if (/^https?:\/\//.test(page.next)) {
    const url = new URL(page.next)
    return `${url.pathname}${url.search}`
  }
  return page.next
}

function pageTotal<T>(pages: Array<Page<T> | T[]> | undefined, rows: T[]) {
  const first = pages?.[0]
  return first && !Array.isArray(first) ? first.count : rows.length
}

function summarySubject(row: CorrespondenceSummary) {
  return row.subject || '(No subject)'
}

function summarySnippet(row: CorrespondenceSummary) {
  return row.snippet
}

interface MessageWorkspaceProps {
  requestNumber: string
  roles: StaffRole[]
  renderDetail: (detail: CorrespondenceDetail) => ReactNode
}

/**
 * The signed-in username, used only to scope this component's saved search box
 * so two people sharing a machine do not see each other's draft. Read from the
 * session here rather than threaded down from the router: as a prop it was
 * drilled through four components, each re-declaring a default of
 * 'local-test-user' -- a test fixture value sitting in production signatures,
 * where one forgotten prop silently wrote under another user's key.
 */
function useSearchScope(): string {
  const me = useMe()
  return me.data?.username ?? 'anonymous'
}

export function MessageWorkspace(props: MessageWorkspaceProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const previousRequestNumber = useRef(props.requestNumber)
  const scope = useSearchScope()

  useEffect(() => {
    if (previousRequestNumber.current === props.requestNumber) return
    previousRequestNumber.current = props.requestNumber
    const next = new URLSearchParams(searchParams)
    next.delete('message')
    next.delete(messageParam('thread'))
    setSearchParams(next, { replace: true })
  }, [props.requestNumber, searchParams, setSearchParams])

  return (
    <RequestMessageWorkspace key={`${scope}:${props.requestNumber}`} {...props} />
  )
}

function RequestMessageWorkspace({
  requestNumber,
  roles,
  renderDetail,
}: MessageWorkspaceProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const toast = useToast()
  const searchScope = useSearchScope()
  const storageKey = `message-search:${searchScope}:${requestNumber}`
  const [searchInput, setSearchInput] = useState(
    () => sessionStorage.getItem(storageKey) ?? '',
  )
  const [searchTerm, setSearchTerm] = useState(
    () => sessionStorage.getItem(storageKey) ?? '',
  )
  const [stagedCount, setStagedCount] = useState(0)
  const [visibleMessagePage, setVisibleMessagePage] = useState(0)
  const [visibleThreadPage, setVisibleThreadPage] = useState(0)
  const messagePagingGeneration = useRef(0)
  const threadPagingGeneration = useRef(0)
  const messagePagingInFlight = useRef<number | null>(null)
  const threadPagingInFlight = useRef<number | null>(null)
  const [workOverrides, setWorkOverrides] = useState<
    Record<string, MessageWorkItem>
  >({})
  const [checkedWork, setCheckedWork] = useState<Record<string, MessageWorkItem>>({})
  const [bulkAction, setBulkAction] = useState<WorkAction>('read')
  const [noActionOpen, setNoActionOpen] = useState(false)
  const [noActionCategory, setNoActionCategory] = useState('informational_only')
  const [noActionReason, setNoActionReason] = useState('')
  const [loadAssigneeChoices, setLoadAssigneeChoices] = useState(false)
  const canManageInboundWork = roles.includes('records_officer')

  const mode = searchParams.get(messageParam('mode')) === 'threads'
    ? 'threads'
    : 'messages'
  const selectedId = searchParams.get('message') ?? ''
  const selectedThread = searchParams.get(messageParam('thread')) ?? ''
  const sort = searchParams.get(messageParam('sort')) === 'oldest'
    ? 'oldest'
    : 'newest'
  const filters = useMemo(() => Object.fromEntries(
    FILTER_KEYS.map((key) => [key, searchParams.get(messageParam(key)) ?? '']),
  ) as Record<FilterKey, string>, [searchParams])

  function updateParams(changes: Record<string, string | null>) {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    setSearchParams(next)
  }

  function updateFilter(key: FilterKey, value: string) {
    updateParams({
      [messageParam(key)]: value || null,
      message: null,
      [messageParam('thread')]: null,
    })
  }

  const apiFilters = useMemo(() => {
    const values: Record<string, string> = {}
    for (const key of FILTER_KEYS) {
      if (filters[key]) values[key] = filters[key]
    }
    if (sort === 'oldest') values.sort = sort
    return values
  }, [filters, sort])
  const apiFiltersScope = JSON.stringify(apiFilters)

  const listPath = useMemo(() => {
    const params = new URLSearchParams({ request: requestNumber })
    for (const [key, value] of Object.entries(apiFilters)) params.set(key, value)
    return `/api/staff/correspondence/?${params.toString()}`
  }, [apiFilters, requestNumber])

  const threadMessagesPath = useMemo(() => {
    const params = new URLSearchParams({
      request: requestNumber,
      thread: selectedThread,
    })
    for (const [key, value] of Object.entries(apiFilters)) params.set(key, value)
    return `/api/staff/correspondence/thread-messages/?${params.toString()}`
  }, [apiFilters, requestNumber, selectedThread])

  const messageQueryKey = useMemo(() => [
    'messages', requestNumber, mode, selectedThread, apiFiltersScope, searchTerm,
  ] as const, [apiFiltersScope, mode, requestNumber, searchTerm, selectedThread])
  const messagePagingScope = JSON.stringify(messageQueryKey)
  const fetchMessagePage = useCallback(async (
    pageParam: string | null,
    signal?: AbortSignal,
  ) => {
    if (searchTerm && mode === 'messages') {
      return api<Page<CorrespondenceSummary>>(
        '/api/staff/correspondence/search/',
        {
          method: 'POST',
          body: {
            request: requestNumber,
            ...apiFilters,
            query: searchTerm,
            ...(pageParam ? { cursor: pageParam } : {}),
          },
          fetchAllPages: false,
          signal,
        },
      )
    }
    const path = pageParam
      ?? (mode === 'threads' ? threadMessagesPath : listPath)
    return api<Page<CorrespondenceSummary> | CorrespondenceSummary[]>(path, {
      fetchAllPages: false,
      signal,
    })
  }, [apiFilters, listPath, mode, requestNumber, searchTerm, threadMessagesPath])

  const messages = useInfiniteQuery({
    queryKey: messageQueryKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => fetchMessagePage(pageParam, signal),
    getNextPageParam: pageNext,
    enabled: mode === 'messages' || selectedThread !== '',
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  })

  const threadPath = useMemo(() => {
    const params = new URLSearchParams({ request: requestNumber })
    for (const [key, value] of Object.entries(apiFilters)) params.set(key, value)
    return `/api/staff/correspondence/threads/?${params.toString()}`
  }, [apiFilters, requestNumber])
  const threadQueryKey = useMemo(() => (
    ['message-threads', requestNumber, apiFiltersScope] as const
  ), [apiFiltersScope, requestNumber])
  const threads = useInfiniteQuery({
    queryKey: threadQueryKey,
    initialPageParam: threadPath,
    queryFn: ({ pageParam, signal }) => api<
      Page<CorrespondenceThreadSummary> | CorrespondenceThreadSummary[]
    >(pageParam, { fetchAllPages: false, signal }),
    getNextPageParam: pageNext,
    enabled: mode === 'threads',
    staleTime: Infinity,
  })
  const threadPagingScope = JSON.stringify(threadQueryKey)

  const messageEnabled = mode === 'messages' || selectedThread !== ''
  useEffect(() => {
    if (!messageEnabled) return
    return () => markBoundedInfiniteQueriesStale(queryClient, messageQueryKey)
  }, [messageEnabled, messageQueryKey, queryClient])
  useEffect(() => {
    if (mode !== 'threads') return
    return () => markBoundedInfiniteQueriesStale(queryClient, threadQueryKey)
  }, [mode, queryClient, threadQueryKey])
  const staffUsers = useQuery({
    queryKey: ['staff-users'],
    queryFn: () => api<Page<StaffUserChoice> | StaffUserChoice[]>(
      '/api/staff/staff-users/',
    ),
    enabled: canManageInboundWork && (
      loadAssigneeChoices || filters.assignee !== ''
    ),
  })
  const staffUserRows = staffUsers.data ? unwrap(staffUsers.data) : []

  const messagePages = messages.data?.pages ?? []
  const loadedRows = pageRows(messagePages)
  const boundedPageIndex = Math.min(
    visibleMessagePage,
    Math.max(messagePages.length - 1, 0),
  )
  const rows = messagePages[boundedPageIndex]
    ? unwrap(messagePages[boundedPageIndex])
    : []
  const threadPages = threads.data?.pages ?? []
  const loadedThreadRows = pageRows(threadPages)
  const boundedThreadPageIndex = Math.min(
    visibleThreadPage,
    Math.max(threadPages.length - 1, 0),
  )
  const threadRows = threadPages[boundedThreadPageIndex]
    ? unwrap(threadPages[boundedThreadPageIndex])
    : []
  const total = pageTotal(messagePages, loadedRows)
  const shownBefore = messagePages
    .slice(0, boundedPageIndex)
    .reduce((count, page) => count + unwrap(page).length, 0)
  const shownFrom = rows.length > 0 ? shownBefore + 1 : 0
  const shownThrough = shownBefore + rows.length
  const threadTotal = pageTotal(threadPages, loadedThreadRows)
  const threadsShownBefore = threadPages
    .slice(0, boundedThreadPageIndex)
    .reduce((count, page) => count + unwrap(page).length, 0)
  const threadsShownFrom = threadRows.length > 0 ? threadsShownBefore + 1 : 0
  const threadsShownThrough = threadsShownBefore + threadRows.length

  useEffect(() => {
    messagePagingGeneration.current += 1
    messagePagingInFlight.current = null
    setStagedCount(0)
    setVisibleMessagePage(0)
  }, [messagePagingScope])

  useEffect(() => {
    threadPagingGeneration.current += 1
    threadPagingInFlight.current = null
    setVisibleThreadPage(0)
  }, [threadPagingScope])

  useEffect(() => {
    if (mode !== 'messages') return undefined
    if (!messages.isSuccess) return undefined
    let cancelled = false
    const checkForArrivals = async () => {
      try {
        const firstPage = await fetchMessagePage(null)
        if (cancelled) return
        const count = Array.isArray(firstPage) ? firstPage.length : firstPage.count
        const added = count - total
        if (added > 0) {
          setStagedCount((current) => Math.max(current, added))
        }
      } catch {
        // The visible query keeps its own error state; focus checks are advisory.
      }
    }
    window.addEventListener('focus', checkForArrivals)
    return () => {
      cancelled = true
      window.removeEventListener('focus', checkForArrivals)
    }
  }, [fetchMessagePage, messages.isSuccess, mode, total])

  const detail = useQuery({
    queryKey: correspondenceDetailQueryKey(requestNumber, selectedId),
    queryFn: () => api<CorrespondenceDetail>(
      `/api/staff/correspondence/${selectedId}/?request=${encodeURIComponent(requestNumber)}`,
      { fetchAllPages: false },
    ),
    enabled: selectedId !== '',
  })

  const selectedSummary = rows.find((row) => row.id === selectedId)
  const activeWork = workOverrides[selectedId]
    ?? detail.data?.work_item
    ?? selectedSummary?.work_item
    ?? null

  const workMutation = useMutation({
    mutationFn: ({
      action,
      work,
    }: {
      action: WorkAction
      work: MessageWorkItem
      messageId: string
    }) => {
      const body = action === 'read' || action === 'unread'
        ? { idempotency_key: idempotencyKey(action) }
        : action === 'waiting' || action === 'handled'
          ? {
              disposition: action,
              expected_version: work.version,
              idempotency_key: idempotencyKey(action),
            }
          : {
              expected_version: work.version,
              idempotency_key: idempotencyKey(action),
            }
      if (action === 'read') {
        return api<MessageWorkItem>(
          `/api/staff/work-items/${work.id}/read/`,
          { method: 'POST', body },
        )
      }
      if (action === 'unread') {
        return api<MessageWorkItem>(
          `/api/staff/work-items/${work.id}/unread/`,
          { method: 'POST', body },
        )
      }
      if (action === 'reopen') {
        return api<MessageWorkItem>(
          `/api/staff/work-items/${work.id}/reopen/`,
          { method: 'POST', body },
        )
      }
      return api<MessageWorkItem>(
        `/api/staff/work-items/${work.id}/disposition/`,
        { method: 'POST', body },
      )
    },
    onSuccess: async (updated, variables) => {
      setWorkOverrides((current) => ({
        ...current,
        [variables.messageId]: updated,
      }))
      await Promise.all([
        invalidateBoundedInfiniteQueries(queryClient, ['messages', requestNumber]),
        invalidateBoundedInfiniteQueries(queryClient, ['message-threads', requestNumber]),
        queryClient.invalidateQueries({ queryKey: ['work-item-counts'] }),
        queryClient.invalidateQueries({
          queryKey: ['case-workspace-summary', requestNumber],
        }),
        queryClient.invalidateQueries({
          queryKey: correspondenceDetailQueryKey(
            requestNumber,
            variables.messageId,
          ),
        }),
      ])
      toast('success', 'Message work state updated.')
    },
    onError: (error) => toast('error', error.message),
  })

  const noAction = useMutation({
    mutationFn: ({ work }: { work: MessageWorkItem; messageId: string }) => api<MessageWorkItem>(
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
    onSuccess: async (updated, variables) => {
      setWorkOverrides((current) => ({
        ...current,
        [variables.messageId]: updated,
      }))
      setNoActionOpen(false)
      setNoActionReason('')
      await Promise.all([
        invalidateBoundedInfiniteQueries(queryClient, ['messages', requestNumber]),
        invalidateBoundedInfiniteQueries(queryClient, ['message-threads', requestNumber]),
        queryClient.invalidateQueries({ queryKey: ['work-item-counts'] }),
        queryClient.invalidateQueries({
          queryKey: ['case-workspace-summary', requestNumber],
        }),
        queryClient.invalidateQueries({
          queryKey: correspondenceDetailQueryKey(
            requestNumber,
            variables.messageId,
          ),
        }),
      ])
      toast('success', 'No-action disposition recorded with its reason.')
    },
    onError: (error) => toast('error', error.message),
  })

  const bulk = useMutation({
    mutationFn: () => api<{ results: unknown[] }>(
      '/api/staff/work-items/bulk-metadata/',
      {
        method: 'POST',
        body: {
          items: Object.values(checkedWork).slice(0, 50).map((work) => ({
            work_item_id: work.id,
            action: bulkAction,
            expected_version: work.version,
            idempotency_key: idempotencyKey(`bulk-${bulkAction}`),
          })),
        },
      },
    ),
    onSuccess: async () => {
      setCheckedWork({})
      await Promise.all([
        invalidateBoundedInfiniteQueries(queryClient, ['messages', requestNumber]),
        invalidateBoundedInfiniteQueries(queryClient, ['message-threads', requestNumber]),
        queryClient.invalidateQueries({ queryKey: ['work-item-counts'] }),
        queryClient.invalidateQueries({
          queryKey: ['case-workspace-summary', requestNumber],
        }),
        queryClient.invalidateQueries({
          queryKey: ['correspondence-detail', requestNumber],
        }),
      ])
      toast('success', 'Bulk message metadata updated.')
    },
    onError: (error) => toast('error', error.message),
  })

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    const term = searchInput.trim().slice(0, 200)
    sessionStorage.setItem(storageKey, term)
    setSearchTerm(term)
    updateParams({ message: null })
  }

  function clearSearch() {
    sessionStorage.removeItem(storageKey)
    setSearchInput('')
    setSearchTerm('')
  }

  function selectMessage(row: CorrespondenceSummary) {
    updateParams({ message: row.id })
  }

  function toggleChecked(row: CorrespondenceSummary, checked: boolean) {
    if (!row.work_item) return
    setCheckedWork((current) => {
      if (!checked) {
        const next = { ...current }
        delete next[row.work_item!.id]
        return next
      }
      if (Object.keys(current).length >= 50) return current
      return { ...current, [row.work_item!.id]: row.work_item! }
    })
  }

  async function showOlderMessagePage() {
    updateParams({ message: null })
    if (boundedPageIndex < messagePages.length - 1) {
      setVisibleMessagePage(boundedPageIndex + 1)
      return
    }
    const generation = messagePagingGeneration.current
    if (messagePagingInFlight.current === generation) return
    messagePagingInFlight.current = generation
    try {
      const result = await messages.fetchNextPage()
      if (
        messagePagingGeneration.current !== generation
        || messagePagingInFlight.current !== generation
      ) return
      setVisibleMessagePage(Math.max((result.data?.pages.length ?? 1) - 1, 0))
    } finally {
      if (messagePagingInFlight.current === generation) {
        messagePagingInFlight.current = null
      }
    }
  }

  function showNewerMessagePage() {
    updateParams({ message: null })
    setVisibleMessagePage(Math.max(boundedPageIndex - 1, 0))
  }

  async function showOlderThreadPage() {
    if (boundedThreadPageIndex < threadPages.length - 1) {
      setVisibleThreadPage(boundedThreadPageIndex + 1)
      return
    }
    const generation = threadPagingGeneration.current
    if (threadPagingInFlight.current === generation) return
    threadPagingInFlight.current = generation
    try {
      const result = await threads.fetchNextPage()
      if (
        threadPagingGeneration.current !== generation
        || threadPagingInFlight.current !== generation
      ) return
      setVisibleThreadPage(Math.max((result.data?.pages.length ?? 1) - 1, 0))
    } finally {
      if (threadPagingInFlight.current === generation) {
        threadPagingInFlight.current = null
      }
    }
  }

  function showNewerThreadPage() {
    setVisibleThreadPage(Math.max(boundedThreadPageIndex - 1, 0))
  }

  async function applyStagedMessages() {
    setStagedCount(0)
    setVisibleMessagePage(0)
    if (sort === 'oldest') {
      updateParams({
        [messageParam('sort')]: null,
        message: null,
      })
      return
    }
    await Promise.all([
      invalidateBoundedInfiniteQueries(queryClient, ['messages', requestNumber]),
      invalidateBoundedInfiniteQueries(queryClient, ['message-threads', requestNumber]),
    ])
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-line bg-surface-sunken p-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex rounded-lg border border-line bg-surface-raised p-1">
            <Button
              variant={mode === 'messages' ? 'secondary' : 'ghost'}
              aria-pressed={mode === 'messages'}
              onClick={() => updateParams({
                [messageParam('mode')]: null,
                [messageParam('thread')]: null,
                message: null,
              })}
            >
              Messages
            </Button>
            <Button
              variant={mode === 'threads' ? 'secondary' : 'ghost'}
              aria-pressed={mode === 'threads'}
              onClick={() => updateParams({
                [messageParam('mode')]: 'threads',
                message: null,
              })}
            >
              Threads
            </Button>
          </div>
          <form className="flex min-w-0 flex-1 flex-wrap items-end gap-2" onSubmit={submitSearch}>
            <div className="min-w-52 flex-1">
              <Field label="Search message content">
              <Input
                value={searchInput}
                maxLength={200}
                disabled={mode === 'threads'}
                onChange={(event) => setSearchInput(event.target.value)}
                placeholder="Search approved or received content"
              />
              </Field>
            </div>
            <Button type="submit" disabled={mode === 'threads'}>
              Search messages
            </Button>
            {searchTerm && (
              <Button type="button" variant="ghost" onClick={clearSearch}>
                Clear search
              </Button>
            )}
          </form>
        </div>

        <details className="mt-3">
          <summary className="cursor-pointer font-heading text-sm font-semibold text-ink">
            Filters and sort
          </summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Direction">
              <Select value={filters.direction} onChange={(event) => updateFilter('direction', event.target.value)}>
                <option value="">Any direction</option>
                <option value="inbound">Incoming</option>
                <option value="outbound">Outbound</option>
              </Select>
            </Field>
            <Field label="Message kind">
              <Select value={filters.kind} onChange={(event) => updateFilter('kind', event.target.value)}>
                <option value="">Any kind</option>
                {KINDS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </Select>
            </Field>
            <Field label="Message status">
              <Select value={filters.status} onChange={(event) => updateFilter('status', event.target.value)}>
                <option value="">Any status</option>
                {['draft', 'approved', 'queued', 'sent', 'failed', 'cancelled', 'received', 'unmatched', 'quarantined'].map((value) => (
                  <option key={value} value={value}>{CORRESPONDENCE_STATUS_LABELS[value] ?? value}</option>
                ))}
              </Select>
            </Field>
            {canManageInboundWork && (
              <Field label="Personal read state">
                <Select value={filters.read_state} onChange={(event) => updateFilter('read_state', event.target.value)}>
                  <option value="">Read or unread</option>
                  <option value="unread">Unread</option>
                  <option value="read">Read</option>
                </Select>
              </Field>
            )}
            {canManageInboundWork && (
              <Field label="Team disposition">
                <Select value={filters.work_state} onChange={(event) => updateFilter('work_state', event.target.value)}>
                  <option value="">Any disposition</option>
                  {Object.entries(WORK_DISPOSITION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </Select>
              </Field>
            )}
            <Field label="Delivery state">
              <Select value={filters.delivery_state} onChange={(event) => updateFilter('delivery_state', event.target.value)}>
                <option value="">Any delivery state</option>
                {Object.entries(DELIVERY_STATUS_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </Select>
            </Field>
            <Field label="Matching state">
              <Select value={filters.matching_state} onChange={(event) => updateFilter('matching_state', event.target.value)}>
                <option value="">Any matching state</option>
                {['matched', 'unmatched', 'candidate', 'ambiguous', 'quarantined', 'duplicate'].map((value) => <option key={value} value={value}>{value}</option>)}
              </Select>
            </Field>
            <Field label="Attachments">
              <Select value={filters.has_attachments} onChange={(event) => updateFilter('has_attachments', event.target.value)}>
                <option value="">With or without</option>
                <option value="true">Has attachments</option>
                <option value="false">No attachments</option>
              </Select>
            </Field>
            <Field label="From date">
              <Input type="date" value={filters.date_from} onChange={(event) => updateFilter('date_from', event.target.value)} />
            </Field>
            <Field label="Through date">
              <Input type="date" value={filters.date_to} onChange={(event) => updateFilter('date_to', event.target.value)} />
            </Field>
            {canManageInboundWork && (
              <Field label="Assignee">
                <Select
                  value={filters.assignee}
                  onFocus={() => setLoadAssigneeChoices(true)}
                  onChange={(event) => updateFilter('assignee', event.target.value)}
                >
                  <option value="">Any assignee</option>
                  {staffUserRows.map((user) => (
                    <option key={user.id} value={String(user.id)}>
                      {user.display_name || user.username}
                    </option>
                  ))}
                </Select>
                {staffUsers.isError && <ErrorNote error={staffUsers.error} />}
              </Field>
            )}
            <Field label="Sort order">
              <Select value={sort} onChange={(event) => updateParams({ [messageParam('sort')]: event.target.value === 'oldest' ? 'oldest' : null, message: null })}>
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
              </Select>
            </Field>
          </div>
        </details>
      </div>

      <div className="min-h-10">
        <span
          role="status"
          aria-label="New message updates"
          aria-live="polite"
          aria-atomic="true"
          className="sr-only"
        >
          {stagedCount > 0
            ? `${stagedCount} new ${stagedCount === 1 ? 'message is' : 'messages are'} available.`
            : ''}
        </span>
        {stagedCount > 0 && (
          <Button
            variant="secondary"
            aria-label={`Show ${stagedCount} new ${stagedCount === 1 ? 'message' : 'messages'}`}
            onClick={() => void applyStagedMessages()}
          >
            Show {stagedCount} new {stagedCount === 1 ? 'message' : 'messages'}
          </Button>
        )}
      </div>

      {mode === 'threads' && !selectedThread && (
        <div className="space-y-2">
          {threads.isPending && <Spinner label="Loading thread summaries…" />}
          {threads.isError && <ErrorNote error={threads.error} />}
          {threadRows.map((thread) => (
            <button
              type="button"
              key={thread.id}
              onClick={() => updateParams({ [messageParam('thread')]: thread.id })}
              className="flex w-full items-center justify-between rounded-lg border border-line bg-surface-raised p-3 text-left hover:border-line-strong"
            >
              <span>
                <span className="block font-heading font-semibold text-ink">{thread.subject}</span>
                <span className="text-xs text-ink-muted">Display-only provider thread</span>
              </span>
              <Badge tone="blue">{thread.message_count} messages</Badge>
            </button>
          ))}
          {threadRows.length > 0 && (
            <p className="text-sm text-ink-muted">
              Showing {threadsShownFrom}–{threadsShownThrough} of {threadTotal} threads
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {boundedThreadPageIndex > 0 && (
              <Button
                type="button"
                variant="ghost"
                aria-label="Show newer threads"
                onClick={showNewerThreadPage}
              >
                Show newer
              </Button>
            )}
            {(boundedThreadPageIndex < threadPages.length - 1 || threads.hasNextPage) && (
              <Button
                type="button"
                variant="ghost"
                aria-label="Load more threads"
                onClick={() => void showOlderThreadPage()}
                disabled={threads.isFetchingNextPage}
              >
                {threads.isFetchingNextPage ? 'Loading more…' : 'Load more'}
              </Button>
            )}
          </div>
        </div>
      )}

      {(mode === 'messages' || selectedThread) && (
        <>
          {mode === 'threads' && selectedThread && (
            <Button variant="ghost" onClick={() => updateParams({ [messageParam('thread')]: null, message: null })}>
              Back to thread summaries
            </Button>
          )}
          {messages.isPending && <Spinner label="Loading message summaries…" />}
          {messages.isError && <ErrorNote error={messages.error} />}
          {messages.isSuccess && rows.length === 0 && (
            <EmptyState>No messages match these filters.</EmptyState>
          )}

          {Object.keys(checkedWork).length > 0 && (
            <div className="flex flex-wrap items-end gap-2 rounded-lg border border-line-strong bg-surface-sunken p-3">
              <span className="font-heading text-sm font-semibold text-ink">
                {Object.keys(checkedWork).length} selected (maximum 50)
              </span>
              <Field label="Bulk metadata action">
                <Select value={bulkAction} onChange={(event) => setBulkAction(event.target.value as WorkAction)}>
                  <option value="read">Mark read</option>
                  <option value="unread">Mark unread</option>
                  <option value="waiting">Set waiting</option>
                  <option value="handled">Set handled</option>
                  <option value="reopen">Reopen</option>
                </Select>
              </Field>
              <Button onClick={() => bulk.mutate()} disabled={bulk.isPending}>
                {bulk.isPending ? 'Updating…' : 'Apply metadata'}
              </Button>
              {bulk.isError && <ErrorNote error={bulk.error} />}
            </div>
          )}

          <div className="grid gap-4 xl:grid-cols-[minmax(18rem,0.8fr)_minmax(28rem,1.2fr)]">
            <div className="space-y-2" role="region" aria-label="Message summaries">
              {rows.map((row) => {
                const isSelected = row.id === selectedId
                const checked = row.work_item
                  ? Boolean(checkedWork[row.work_item.id])
                  : false
                return (
                  <div
                    key={row.id}
                    className={`rounded-lg border bg-surface-raised p-3 ${isSelected ? 'border-line-strong ring-2 ring-focus' : 'border-line'}`}
                  >
                    <div className="flex items-start gap-2">
                      {canManageInboundWork && row.work_item && (
                        <input
                          type="checkbox"
                          aria-label={`Select ${summarySubject(row)} for bulk metadata`}
                          checked={checked}
                          disabled={!checked && Object.keys(checkedWork).length >= 50}
                          onChange={(event) => toggleChecked(row, event.target.checked)}
                        />
                      )}
                      <button
                        type="button"
                        aria-label={`Open ${summarySubject(row)}`}
                        aria-pressed={isSelected}
                        onClick={() => selectMessage(row)}
                        className="min-w-0 flex-1 text-left"
                      >
                        <span className="flex flex-wrap items-center gap-2">
                          <Badge tone={row.direction === 'inbound' ? 'blue' : 'green'}>
                            {row.direction === 'inbound' ? 'Incoming' : 'Outbound'}
                          </Badge>
                          <Badge tone={correspondenceStatusTone(row.status)}>
                            {CORRESPONDENCE_STATUS_LABELS[row.status] ?? row.status}
                          </Badge>
                          {row.work_item && !row.work_item.is_read && <Badge tone="amber">Unread</Badge>}
                        </span>
                        <span className="mt-2 block truncate font-heading font-semibold text-ink">
                          {summarySubject(row)}
                        </span>
                        <span className="mt-1 line-clamp-2 block text-sm text-ink-muted">{summarySnippet(row)}</span>
                        <span className="mt-2 block text-xs text-ink-muted">
                          {CORRESPONDENCE_KIND_LABELS[row.kind]} · {row.created_at.slice(0, 16).replace('T', ' ')}
                          {row.attachment_count ? ` · ${row.attachment_count} attachment${row.attachment_count === 1 ? '' : 's'}` : ''}
                        </span>
                      </button>
                    </div>
                  </div>
                )
              })}
              {rows.length > 0 && (
                <p className="text-sm text-ink-muted">
                  Showing {shownFrom}–{shownThrough} of {total} messages
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                {boundedPageIndex > 0 && (
                  <Button
                    type="button"
                    variant="ghost"
                    aria-label="Show newer messages"
                    onClick={showNewerMessagePage}
                  >
                    Show newer
                  </Button>
                )}
                {(boundedPageIndex < messagePages.length - 1 || messages.hasNextPage) && (
                  <Button
                    type="button"
                    variant="ghost"
                    aria-label="Load more messages"
                    disabled={messages.isFetchingNextPage}
                    onClick={() => void showOlderMessagePage()}
                  >
                    {messages.isFetchingNextPage ? 'Loading more…' : 'Load more'}
                  </Button>
                )}
              </div>
            </div>

            <div aria-live="polite">
              {!selectedId && rows.length > 0 && (
                <EmptyState>Select one message to load its complete legal record.</EmptyState>
              )}
              {selectedId && detail.isPending && <Spinner label="Loading selected message…" />}
              {selectedId && detail.isError && <ErrorNote error={detail.error} />}
              {detail.data && (
                <div className="space-y-3">
                  {canManageInboundWork && activeWork && (
                    <div className="rounded-lg border border-line bg-surface-sunken p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone={activeWork.is_read ? 'neutral' : 'amber'}>
                          Personal: {activeWork.is_read ? 'Read' : 'Unread'}
                        </Badge>
                        <Badge tone={activeWork.disposition === 'needs_attention' ? 'red' : 'blue'}>
                          Team: {WORK_DISPOSITION_LABELS[activeWork.disposition]}
                        </Badge>
                        {activeWork.assignee_username && <span className="text-xs text-ink-muted">Assigned to {activeWork.assignee_username}</span>}
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Button variant="ghost" onClick={() => workMutation.mutate({ action: activeWork.is_read ? 'unread' : 'read', work: activeWork, messageId: selectedId })} disabled={workMutation.isPending}>
                          {activeWork.is_read ? 'Mark unread' : 'Mark read'}
                        </Button>
                        {activeWork.disposition === 'needs_attention' && (
                          <Button variant="ghost" onClick={() => workMutation.mutate({ action: 'waiting', work: activeWork, messageId: selectedId })} disabled={workMutation.isPending}>Set waiting</Button>
                        )}
                        {['needs_attention', 'waiting'].includes(activeWork.disposition) && (
                          <Button variant="ghost" onClick={() => workMutation.mutate({ action: 'handled', work: activeWork, messageId: selectedId })} disabled={workMutation.isPending}>Mark handled</Button>
                        )}
                        {!['needs_attention', 'waiting'].includes(activeWork.disposition) && (
                          <Button variant="ghost" onClick={() => workMutation.mutate({ action: 'reopen', work: activeWork, messageId: selectedId })} disabled={workMutation.isPending}>Reopen team work</Button>
                        )}
                        {roles.includes('records_officer') && ['needs_attention', 'waiting'].includes(activeWork.disposition) && (
                          <Button variant="ghost" onClick={() => setNoActionOpen(true)} disabled={workMutation.isPending || noAction.isPending}>No action required</Button>
                        )}
                      </div>
                    </div>
                  )}
                  {renderDetail(detail.data)}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      <NoActionModal
        open={noActionOpen}
        onClose={() => setNoActionOpen(false)}
        onSubmit={() => {
          if (activeWork) {
            noAction.mutate({ work: activeWork, messageId: selectedId })
          }
        }}
        category={noActionCategory}
        onCategoryChange={setNoActionCategory}
        reason={noActionReason}
        onReasonChange={setNoActionReason}
        pending={noAction.isPending}
        error={noAction.isError ? noAction.error : null}
      />
    </div>
  )
}
