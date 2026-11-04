// ABOUTME: Human review workspace for verified raster previews, responsiveness,
// ABOUTME: exemption-backed redactions, second review, and immutable burn-in.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { useSearchParams } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import type { StaffRole } from '../api/auth'
import { DiscardBatchCard } from '../case/BatchReview'
import { RecordClusters } from '../case/RecordClusters'
import { RecordHandlingPanel } from '../case/RecordHandlingPanel'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  FilterChip,
  Input,
  MIN_REASON_LENGTH,
  Modal,
  Pager,
  Select,
  Spinner,
  Table,
  Textarea,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
  useToast,
} from '../ui'
import type { Column } from '../ui/Table'

interface RecordItem {
  id: string
  request_number: string
  source_system: string
  custodian: string
  custodians: string[]
  email_from: string
  email_to: string
  email_cc: string
  email_sent_at: string | null
  email_message_id: string
  email_in_reply_to: string
  email_sender_domain: string
  email_auto_submitted: string
  email_list_id: string
  email_precedence: string
  email_has_calendar_part: boolean
  tags: string[]
  advisory_suggested_state: string | null
  advisory_confidence: number | null
  original_filename: string
  sha256: string
  review_state: string
  reviewed_by_username: string | null
  reviewed_at: string | null
  redaction_count: number
  second_review_required: boolean
  second_reviewed_by_username: string | null
  second_reviewed_at: string | null
  redacted_artifact_ready: boolean
  withholding_exemption_citations: string[]
  created_at: string
  content_type: string
  content_length: number
  handling: 'preview' | 'convert' | 'media' | 'opaque'
  handling_label: string
  handling_detail: string
  derivative_state: string
  derivative_detail: string
  reviewable: boolean
}

interface Exemption {
  id: number
  citation: string
  short_name: string
  requires_second_review: boolean
  active: boolean
}

interface Redaction {
  id: number
  record_item: string
  page_number: number
  x0: number
  y0: number
  x1: number
  y1: number
  exemption: number
  citation: string
  exemption_name: string
  requires_second_review: boolean
  explanation: string
  created_by_username: string | null
  created_at: string
  voided_at: string | null
  void_reason: string
}

interface PreviewMetadata {
  page_count: number
}

interface ResponsivenessAdvisoryRun {
  id: string
  trigger: 'manual' | 'automatic'
  state: 'pending' | 'running' | 'complete' | 'failed'
  model_identifier: string
  input_sha256: string
  input_truncated: boolean
  applied: false
  proposal: {
    suggested_state?: 'responsive' | 'non_responsive' | 'uncertain'
    confidence?: number
    matched_terms?: string[]
    rationale?: string
  }
  failure_detail: string
}

interface ResponsivenessAdvisoryLatest {
  enabled: boolean
  latest: ResponsivenessAdvisoryRun | null
}

interface RedactionProposalDecision {
  outcome: 'accepted' | 'rejected'
  decided_by_username: string
  reason: string
  redaction: number | null
  decided_at: string
}

interface RedactionProposal extends NormalizedBox {
  id: string
  advisory_run: string
  record_item: string
  page_number: number
  exemption: number
  citation: string
  exemption_name: string
  requires_second_review: boolean
  confidence: number
  rationale: string
  source_span_ids: string[]
  source_text_sha256: string
  created_at: string
  decision: RedactionProposalDecision | null
}

interface RedactionAdvisoryLatest {
  enabled: boolean
  latest: ResponsivenessAdvisoryRun | null
  proposals: RedactionProposal[]
}

interface NormalizedBox {
  x0: number
  y0: number
  x1: number
  y1: number
}

const REVIEW_STATES = [
  { value: 'unreviewed', label: 'Unreviewed' },
  { value: 'responsive', label: 'Responsive' },
  { value: 'non_responsive', label: 'Non-responsive' },
  { value: 'duplicate', label: 'Duplicate' },
  { value: 'withheld', label: 'Withheld (fully exempt)' },
  // Set only by superseding a collection; the default list leaves these out.
  { value: 'superseded', label: 'Superseded' },
] as const

const HUMAN_REVIEW_STATES = REVIEW_STATES.filter(
  (state) => state.value !== 'unreviewed' && state.value !== 'superseded',
)

const RECORD_PAGE_SIZE = 50

const SORT_OPTIONS = [
  { value: 'newest', label: 'Newest first' },
  { value: 'oldest', label: 'Oldest first' },
  { value: 'filename', label: 'Filename A–Z' },
  { value: '-filename', label: 'Filename Z–A' },
  { value: 'largest', label: 'Largest first' },
  { value: 'smallest', label: 'Smallest first' },
  { value: '-sent', label: 'Sent newest first' },
  { value: 'sent', label: 'Sent oldest first' },
  { value: 'sender', label: 'Sender domain A–Z' },
  { value: 'custodian', label: 'Custodian A–Z' },
  { value: '-confidence', label: 'Highest AI confidence' },
  { value: 'confidence', label: 'Lowest AI confidence' },
]

// Deterministic facts about the message, never decisions about it. The server
// derives them from stored headers; nothing here is a legal conclusion.
const TAG_LABELS: Record<string, string> = {
  auto_reply: 'Automatic reply',
  calendar: 'Calendar',
  bulk_list: 'Bulk or list mail',
  external_sender: 'External sender',
  counsel_domain: 'District counsel',
  privilege_marker: 'Privilege marker',
}

const TAG_TONE: Record<string, 'neutral' | 'green' | 'amber' | 'blue' | 'red'> = {
  auto_reply: 'neutral',
  calendar: 'blue',
  bulk_list: 'neutral',
  external_sender: 'blue',
  counsel_domain: 'amber',
  privilege_marker: 'amber',
}

const TAG_ORDER = [
  'auto_reply',
  'calendar',
  'bulk_list',
  'external_sender',
  'counsel_domain',
  'privilege_marker',
]

// The two tags that put a record in the privilege queue, mirroring
// `record_tags.PRIVILEGE_TAGS`. They decide nothing; they mean a human has to
// read the record before anything ships.
const PRIVILEGE_TAGS = ['counsel_domain', 'privilege_marker']

// A record withheld in full carries an exemption citation, and one marked a
// duplicate carries a judgement about which copy is the record. Both are
// one-at-a-time decisions -- the service refuses a batch that carries either
// -- so neither is ever staged for a bulk decision here.
const ONE_AT_A_TIME = 'Withheld, duplicate, and superseded records are reviewed one at a time'
function bulkEligible(row: RecordItem): boolean {
  return (
    row.review_state !== 'withheld'
    && row.review_state !== 'duplicate'
    && row.review_state !== 'superseded'
  )
}

const GROUP_OPTIONS = [
  { value: '', label: 'No grouping' },
  { value: 'tag', label: 'Group by tag' },
  { value: 'sender_domain', label: 'Group by sender domain' },
  { value: 'custodian', label: 'Group by custodian' },
  { value: 'confidence', label: 'Group by AI confidence' },
  { value: 'cluster', label: 'Group by cluster' },
]

const CONFIDENCE_OPTIONS = [
  { value: '', label: 'Any AI confidence' },
  { value: '0.9', label: 'AI confidence ≥ 0.9' },
  { value: '0.7', label: 'AI confidence ≥ 0.7' },
  { value: '0.5', label: 'AI confidence ≥ 0.5' },
]

// Matches the server's cap. One confirmation should cover a set a person could
// plausibly have looked at.
const BULK_REVIEW_MAX = 200

const BULK_REVIEW_STATES = [
  { value: 'responsive', label: 'Responsive' },
  { value: 'non_responsive', label: 'Non-responsive' },
]

interface FacetRow {
  value: string
  count: number
}

interface BulkSelectionPreview {
  matching: number
  eligible_count: number
  excluded: Record<'withheld' | 'duplicate' | 'superseded' | 'already' | 'redacted', number>
  selection_sha256: string
  privilege_count: number
  max_items: number
}

// [one, many]: what each skipped group is, said as a sentence.
const EXCLUDED_LABELS: Record<keyof BulkSelectionPreview['excluded'], [string, string]> = {
  already: ['record already has this outcome', 'records already have this outcome'],
  withheld: ['record is withheld in full', 'records are withheld in full'],
  duplicate: ['record is marked a duplicate', 'records are marked duplicates'],
  superseded: [
    'record was superseded with its collection',
    'records were superseded with their collection',
  ],
  redacted: ['record carries active redactions', 'records carry active redactions'],
}

interface RecordFacets {
  total: number
  privilege: number
  tags: (FacetRow & { label: string })[]
  sender_domains: FacetRow[]
  custodians: FacetRow[]
}

// Every other cell in this table degrades to an em dash rather than throwing,
// because one absent field in one row should never blank the whole queue.
function asList(values: string[] | undefined): string[] {
  return Array.isArray(values) ? values : []
}

function confidenceBand(value: number | null): string {
  if (value === null) return 'No AI suggestion'
  if (value >= 0.9) return 'AI confidence 0.9 and above'
  if (value >= 0.7) return 'AI confidence 0.7 to 0.9'
  if (value >= 0.5) return 'AI confidence 0.5 to 0.7'
  return 'AI confidence below 0.5'
}

function groupKeys(row: RecordItem, groupBy: string): string[] {
  if (groupBy === 'tag') {
    const tags = asList(row.tags)
    return tags.length ? tags.map((tag) => TAG_LABELS[tag] ?? tag) : ['Untagged']
  }
  if (groupBy === 'sender_domain') return [row.email_sender_domain || 'No sender domain']
  if (groupBy === 'custodian') {
    const names = asList(row.custodians)
    return names.length ? names : ['No custodian recorded']
  }
  if (groupBy === 'confidence') return [confidenceBand(row.advisory_confidence)]
  return ['']
}

const HANDLING_FILTERS = [
  { value: 'preview', label: 'Ready to review' },
  { value: 'convert', label: 'Needs conversion' },
  { value: 'media', label: 'Audio or video' },
  { value: 'opaque', label: 'No renderer' },
]

// Only `preview` can be opened and redacted today. The others are stated
// plainly in the list so a reviewer learns it before spending a click, and so
// a queue that looks short is not mistaken for a queue that is done.
const HANDLING_TONE: Record<string, 'green' | 'amber' | 'blue' | 'red'> = {
  preview: 'green',
  convert: 'amber',
  media: 'blue',
  opaque: 'red',
}

function formatBytes(bytes: number): string {
  if (!bytes) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}

function reviewStateTone(state: string): 'neutral' | 'green' | 'amber' {
  if (state === 'responsive') return 'green'
  if (state === 'withheld') return 'amber'
  return 'neutral'
}

function reviewStateLabel(state: string): string {
  return REVIEW_STATES.find((choice) => choice.value === state)?.label ?? state
}

function truncateHash(hash: string): string {
  return hash.length > 16 ? `${hash.slice(0, 16)}…` : hash
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function pointerPosition(event: ReactPointerEvent<HTMLElement>) {
  const bounds = event.currentTarget.getBoundingClientRect()
  return {
    x: clamp((event.clientX - bounds.left) / bounds.width),
    y: clamp((event.clientY - bounds.top) / bounds.height),
  }
}

function normalizedBox(start: { x: number; y: number }, end: { x: number; y: number }) {
  const round = (value: number) => Number(value.toFixed(4))
  return {
    x0: round(Math.min(start.x, end.x)),
    y0: round(Math.min(start.y, end.y)),
    x1: round(Math.max(start.x, end.x)),
    y1: round(Math.max(start.y, end.y)),
  }
}

function Region({
  box,
  draft = false,
  proposal = false,
}: {
  box: NormalizedBox
  draft?: boolean
  proposal?: boolean
}) {
  return (
    <span
      aria-hidden="true"
      className={`pointer-events-none absolute border-2 ${
        draft
          ? 'border-warning bg-warning/40'
          : proposal
            ? 'border-info bg-info/30'
            : 'border-danger bg-black/70'
      }`}
      style={{
        left: `${box.x0 * 100}%`,
        top: `${box.y0 * 100}%`,
        width: `${(box.x1 - box.x0) * 100}%`,
        height: `${(box.y1 - box.y0) * 100}%`,
      }}
    />
  )
}

export function RecordItems({
  roles = ['reviewer'],
  requestNumber: scopedRequestNumber,
}: {
  roles?: StaffRole[]
  requestNumber?: string
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const urlRequest = scopedRequestNumber ?? searchParams.get('request')?.trim() ?? ''
  const itemTarget = searchParams.get('item')?.trim() ?? ''
  const canReview = roles.includes('reviewer') || roles.includes('records_officer')
  // The search matches correspondent addresses only for a Records Officer;
  // for everyone else those fields are blanked and not searched, and the
  // placeholder must not promise otherwise.
  const canSearchCorrespondents = roles.includes('records_officer')
  const qc = useQueryClient()
  const toast = useToast()
  // The case tab always supplies the request, so there is no free-text request
  // box to debounce any more; the URL is the only source.
  const request = urlRequest
  const activeReviewState = searchParams.get('record_state') ?? ''
  const activeHandling = searchParams.get('handling') ?? ''
  const activeSort = searchParams.get('sort') ?? 'newest'
  const activeSearch = searchParams.get('q') ?? ''
  const activeTag = searchParams.get('tag') ?? ''
  const activeSenderDomain = searchParams.get('sender') ?? ''
  const activeCustodian = searchParams.get('custodian') ?? ''
  const activeConfidence = searchParams.get('confidence') ?? ''
  const activeGroup = searchParams.get('group') ?? ''
  const listPage = Math.max(1, Number(searchParams.get('page') ?? '1') || 1)
  const [searchDraft, setSearchDraft] = useState(activeSearch)
  const [bulkSelection, setBulkSelection] = useState<string[]>([])
  const [bulkState, setBulkState] = useState('')
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false)
  // "Every record this filter matches" rather than rows ticked on this page.
  const [matchingMode, setMatchingMode] = useState(false)
  const [selected, setSelected] = useState<RecordItem | null>(null)
  // Distinct from `listPage`: this is the page *inside* the selected PDF.
  const [pageNumber, setPageNumber] = useState(1)
  const [reviewDraft, setReviewDraft] = useState('responsive')
  const [reviewDraftTouched, setReviewDraftTouched] = useState(false)
  const [withholdingExemption, setWithholdingExemption] = useState('')
  const [redactionExemption, setRedactionExemption] = useState('')
  const [redactionExplanation, setRedactionExplanation] = useState('')
  const [dragStart, setDragStart] = useState<{ x: number; y: number } | null>(null)
  const [draftBox, setDraftBox] = useState<NormalizedBox | null>(null)
  const [selectedProposal, setSelectedProposal] = useState<string | null>(null)
  const [proposalRejectReason, setProposalRejectReason] = useState('')
  const [voidTarget, setVoidTarget] = useState<number | null>(null)
  const [voidReason, setVoidReason] = useState('')
  const hydratedTarget = useRef('')

  // Every filter that narrows the list, in one place, so the list request, the
  // facet counts, and the bulk-review selection all describe the same set.
  function filterParams(): URLSearchParams {
    const params = new URLSearchParams()
    if (request) params.set('request', request)
    if (activeReviewState) params.set('review_state', activeReviewState)
    if (activeHandling) params.set('handling', activeHandling)
    if (activeSearch) params.set('search', activeSearch)
    if (activeTag) params.set('tag', activeTag)
    if (activeSenderDomain) params.set('sender_domain', activeSenderDomain)
    if (activeCustodian) params.set('custodian', activeCustodian)
    if (activeConfidence) params.set('confidence_min', activeConfidence)
    return params
  }

  const filterKey = [
    request, activeReviewState, activeHandling, activeSearch, activeTag,
    activeSenderDomain, activeCustodian, activeConfidence,
  ] as const

  const list = useQuery({
    queryKey: ['record-items', ...filterKey, activeSort, listPage],
    queryFn: () => {
      const params = filterParams()
      // Defaults are left out so an unfiltered view keeps a clean URL and the
      // server's own defaults stay the single source of truth.
      if (activeSort !== 'newest') params.set('ordering', activeSort)
      if (listPage > 1) params.set('page', String(listPage))
      const qs = params.toString()
      return api<Page<RecordItem> | RecordItem[]>(
        `/api/staff/record-items/${qs ? `?${qs}` : ''}`,
        // One page per view, deliberately. The client otherwise walks every
        // `next` link and concatenates, which is fine for a short list but
        // turns a 2,000-item collection into 40 serial round trips before a
        // single row appears -- and then renders all 2,000 at once.
        { fetchAllPages: false },
      )
    },
    placeholderData: (previous) => previous,
  })
  const facets = useQuery({
    queryKey: ['record-item-facets', ...filterKey],
    queryFn: () => {
      const qs = filterParams().toString()
      return api<RecordFacets>(
        `/api/staff/record-items/facets/${qs ? `?${qs}` : ''}`,
      )
    },
    enabled: request !== '',
    placeholderData: (previous) => previous,
  })
  const targetedItem = useQuery({
    queryKey: ['record-item-target', request, itemTarget],
    queryFn: () => api<RecordItem>(
      `/api/staff/record-items/${itemTarget}/${request ? `?request=${encodeURIComponent(request)}` : ''}`,
    ),
    enabled: itemTarget !== '',
  })

  useEffect(() => {
    if (!itemTarget) {
      hydratedTarget.current = ''
      setSelected(null)
      return
    }
    const targetKey = `${request}:${itemTarget}`
    if (hydratedTarget.current === targetKey) return
    const item = targetedItem.data
    if (!item || (request && item.request_number !== request)) return
    hydratedTarget.current = targetKey
    setSelected(item)
    setPageNumber(1)
    setReviewDraft(
      item.review_state === 'unreviewed' ? 'responsive' : item.review_state,
    )
    setReviewDraftTouched(false)
    setWithholdingExemption('')
    setRedactionExemption('')
    setRedactionExplanation('')
    setDraftBox(null)
    setSelectedProposal(null)
    setProposalRejectReason('')
    setDragStart(null)
    setVoidTarget(null)
    setVoidReason('')
  }, [itemTarget, request, targetedItem.data])

  const metadata = useQuery({
    queryKey: ['record-preview-metadata', selected?.id],
    queryFn: () => api<PreviewMetadata>(
      `/api/staff/record-items/${selected!.id}/preview-metadata/`,
    ),
    enabled: selected !== null && canReview && selected.reviewable !== false,
  })
  const exemptions = useQuery({
    queryKey: ['exemptions', 'active', 'record-review'],
    queryFn: () => api<Page<Exemption> | Exemption[]>('/api/staff/exemptions/?active=true'),
    enabled: selected !== null && canReview,
  })
  const redactions = useQuery({
    queryKey: ['redactions', selected?.id],
    queryFn: () => api<Page<Redaction> | Redaction[]>(
      `/api/staff/redactions/?record_item=${selected!.id}`,
    ),
    enabled: selected !== null && canReview,
  })
  const responsivenessAdvisory = useQuery({
    queryKey: ['record-responsiveness-advisory', selected?.id],
    queryFn: () => api<ResponsivenessAdvisoryLatest>(
      `/api/staff/record-items/${selected!.id}/advisory-responsiveness/`,
    ),
    enabled: selected !== null && canReview,
    refetchInterval: (query) => {
      const state = query.state.data?.latest?.state
      return state === 'pending' || state === 'running' ? 2000 : false
    },
  })
  const redactionAdvisory = useQuery({
    queryKey: ['record-redaction-advisory', selected?.id],
    queryFn: () => api<RedactionAdvisoryLatest>(
      `/api/staff/record-items/${selected!.id}/advisory-redactions/`,
    ),
    enabled: selected?.review_state === 'responsive' && canReview,
    refetchInterval: (query) => {
      const state = query.state.data?.latest?.state
      return state === 'pending' || state === 'running' ? 2000 : false
    },
  })

  useEffect(() => {
    const suggestion = responsivenessAdvisory.data?.latest?.proposal.suggested_state
    if (
      selected?.review_state === 'unreviewed'
      && !reviewDraftTouched
      && (suggestion === 'responsive' || suggestion === 'non_responsive')
    ) {
      setReviewDraft(suggestion)
    }
  }, [responsivenessAdvisory.data, reviewDraftTouched, selected?.review_state])

  function updateSelected(updated: RecordItem, message: string) {
    setSelected(updated)
    qc.invalidateQueries({ queryKey: ['record-items'] })
    qc.invalidateQueries({
      queryKey: ['case-workspace-summary', updated.request_number],
    })
    qc.invalidateQueries({
      queryKey: ['case-closure-readiness', updated.request_number],
    })
    toast('success', message)
  }

  function invalidateSelectedCaseSummary() {
    if (!selected?.request_number) return
    qc.invalidateQueries({
      queryKey: ['case-workspace-summary', selected.request_number],
    })
    qc.invalidateQueries({
      queryKey: ['case-closure-readiness', selected.request_number],
    })
  }

  const reviewMutation = useMutation({
    mutationFn: () => api<RecordItem>(
      `/api/staff/record-items/${selected!.id}/review/`,
      {
        method: 'POST',
        body: {
          state: reviewDraft,
          withholding_exemption_ids:
            reviewDraft === 'withheld' && withholdingExemption
              ? [Number(withholdingExemption)]
              : [],
        },
      },
    ),
    onSuccess: (updated) => {
      updateSelected(updated, 'Review outcome recorded.')
      qc.invalidateQueries({ queryKey: ['record-redaction-advisory', selected?.id] })
    },
    onError: (error) => toast('error', error.message),
  })

  // A human decision about records this reviewer selected and could see, not
  // an accept-all of AI output: the request carries only the chosen ids and
  // the chosen outcome, and no advisory proposal takes part in it.
  const hasFilter = Boolean(
    activeReviewState || activeHandling || activeSearch || activeTag
    || activeSenderDomain || activeCustodian || activeConfidence,
  )
  const selectionPreview = useQuery({
    queryKey: ['record-bulk-selection', ...filterKey, bulkState],
    enabled: bulkConfirmOpen && matchingMode && bulkState !== '',
    queryFn: () => {
      const params = filterParams()
      params.set('state', bulkState)
      return api<BulkSelectionPreview>(
        `/api/staff/record-items/bulk-selection/?${params.toString()}`,
      )
    },
    // The count is what the reviewer confirms; never show a stale one.
    staleTime: 0,
    gcTime: 0,
  })
  // A decision over the exact set the preview counted. The server re-counts
  // under lock and refuses if anything changed in between.
  const bulkFilterMutation = useMutation({
    mutationFn: (preview: BulkSelectionPreview) => api<{ reviewed_count: number }>(
      '/api/staff/record-items/bulk-review/',
      {
        method: 'POST',
        body: {
          filter: Object.fromEntries(filterParams()),
          state: bulkState,
          expected_count: preview.eligible_count,
          selection_sha256: preview.selection_sha256,
        },
      },
    ),
    onSuccess: (result) => {
      setMatchingMode(false)
      setBulkState('')
      setBulkConfirmOpen(false)
      qc.invalidateQueries({ queryKey: ['record-items'] })
      qc.invalidateQueries({ queryKey: ['record-item-facets'] })
      if (request) {
        qc.invalidateQueries({ queryKey: ['case-workspace-summary', request] })
        qc.invalidateQueries({ queryKey: ['case-closure-readiness', request] })
      }
      toast('success', `Recorded ${result.reviewed_count} review outcomes.`)
    },
    onError: () => {
      qc.invalidateQueries({ queryKey: ['record-bulk-selection'] })
    },
  })
  const bulkReviewMutation = useMutation({
    mutationFn: (payload: { ids: string[]; state: string }) => api<{
      reviewed_count: number
    }>(
      '/api/staff/record-items/bulk-review/',
      {
        method: 'POST',
        body: { record_item_ids: payload.ids, state: payload.state },
      },
    ),
    onSuccess: (result) => {
      setBulkSelection([])
      setBulkState('')
      qc.invalidateQueries({ queryKey: ['record-items'] })
      qc.invalidateQueries({ queryKey: ['record-item-facets'] })
      if (request) {
        qc.invalidateQueries({ queryKey: ['case-workspace-summary', request] })
        // A bulk decision moves the same needle a single-record decision
        // does, so it has to refresh closure readiness for the same reason
        // `updateSelected` does. Leaving it out let the case read as
        // unfinished after the work was in fact done.
        qc.invalidateQueries({ queryKey: ['case-closure-readiness', request] })
      }
      toast(
        'success',
        `Recorded ${result.reviewed_count} review ${
          result.reviewed_count === 1 ? 'outcome' : 'outcomes'
        }.`,
      )
    },
    onError: (error) => toast('error', error.message),
  })

  const responsivenessAdvisoryMutation = useMutation({
    mutationFn: () => api<ResponsivenessAdvisoryRun>(
      `/api/staff/record-items/${selected!.id}/advisory-responsiveness/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ['record-responsiveness-advisory', selected?.id],
      })
      toast('success', 'Advisory analysis queued. Review state was not changed.')
    },
    onError: (error) => toast('error', error.message),
  })

  const redactionAdvisoryMutation = useMutation({
    mutationFn: () => api<ResponsivenessAdvisoryRun>(
      `/api/staff/record-items/${selected!.id}/advisory-redactions/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['record-redaction-advisory', selected?.id] })
      setSelectedProposal(null)
      setProposalRejectReason('')
      toast('success', 'AI redaction pass queued. No redaction was applied.')
    },
    onError: (error) => toast('error', error.message),
  })

  const createRedactionMutation = useMutation({
    mutationFn: () => api<Redaction>('/api/staff/redactions/', {
      method: 'POST',
      body: {
        record_item: selected!.id,
        page_number: pageNumber,
        ...draftBox!,
        exemption: Number(redactionExemption),
        explanation: redactionExplanation,
      },
    }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ['redactions', selected?.id] })
      qc.invalidateQueries({ queryKey: ['record-items'] })
      invalidateSelectedCaseSummary()
      setSelected((current) => current ? {
        ...current,
        redaction_count: current.redaction_count + 1,
        second_review_required:
          current.second_review_required || created.requires_second_review,
        second_reviewed_by_username: null,
        second_reviewed_at: null,
        redacted_artifact_ready: false,
      } : current)
      setDraftBox(null)
      setRedactionExplanation('')
      toast('success', 'Exemption-backed redaction added.')
    },
    onError: (error) => toast('error', error.message),
  })

  const acceptProposalMutation = useMutation({
    mutationFn: () => api<RedactionProposal>(
      `/api/staff/redaction-proposals/${selectedProposal}/accept/`,
      {
        method: 'POST',
        body: {
          page_number: pageNumber,
          ...draftBox!,
          exemption: Number(redactionExemption),
          explanation: redactionExplanation,
        },
      },
    ),
    onSuccess: (proposal) => {
      qc.invalidateQueries({ queryKey: ['record-redaction-advisory', selected?.id] })
      qc.invalidateQueries({ queryKey: ['redactions', selected?.id] })
      qc.invalidateQueries({ queryKey: ['record-items'] })
      invalidateSelectedCaseSummary()
      setSelected((current) => current ? {
        ...current,
        redaction_count: current.redaction_count + 1,
        second_review_required:
          current.second_review_required || proposal.requires_second_review,
        second_reviewed_by_username: null,
        second_reviewed_at: null,
        redacted_artifact_ready: false,
      } : current)
      setSelectedProposal(null)
      setProposalRejectReason('')
      setDraftBox(null)
      setRedactionExplanation('')
      toast('success', 'Human-edited proposal accepted as a legal redaction.')
    },
    onError: (error) => toast('error', error.message),
  })

  const rejectProposalMutation = useMutation({
    mutationFn: () => api<RedactionProposal>(
      `/api/staff/redaction-proposals/${selectedProposal}/reject/`,
      { method: 'POST', body: { reason: proposalRejectReason } },
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['record-redaction-advisory', selected?.id] })
      setSelectedProposal(null)
      setProposalRejectReason('')
      setDraftBox(null)
      setRedactionExplanation('')
      toast('success', 'Proposal rejected with the human reason retained.')
    },
    onError: (error) => toast('error', error.message),
  })

  const secondReviewMutation = useMutation({
    mutationFn: () => api<RecordItem>(
      `/api/staff/record-items/${selected!.id}/second-review/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: (updated) => updateSelected(updated, 'Second review approved.'),
    onError: (error) => toast('error', error.message),
  })

  const renderMutation = useMutation({
    mutationFn: () => api<RecordItem>(
      `/api/staff/record-items/${selected!.id}/render-redactions/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: (updated) => updateSelected(updated, 'Immutable redacted PDF built.'),
    onError: (error) => toast('error', error.message),
  })

  const voidMutation = useMutation({
    mutationFn: () => api<Redaction>(`/api/staff/redactions/${voidTarget}/void/`, {
      method: 'POST',
      body: { reason: voidReason },
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['redactions', selected?.id] })
      qc.invalidateQueries({ queryKey: ['record-items'] })
      invalidateSelectedCaseSummary()
      setSelected((current) => current ? {
        ...current,
        redaction_count: Math.max(0, current.redaction_count - 1),
        second_reviewed_by_username: null,
        second_reviewed_at: null,
        redacted_artifact_ready: false,
      } : current)
      setVoidTarget(null)
      setVoidReason('')
      toast('success', 'Redaction voided with its evidence retained.')
    },
    onError: (error) => toast('error', error.message),
  })

  function openReview(item: RecordItem) {
    // The row already carries the record, so this hydrates the review state
    // here and marks the target as hydrated. Otherwise the background fetch
    // of the same record by id would land a moment later and re-run the
    // hydration, wiping whatever the reviewer had done in the meantime.
    hydratedTarget.current = `${request}:${item.id}`
    setSelected(item)
    setPageNumber(1)
    setReviewDraft(item.review_state === 'unreviewed' ? 'responsive' : item.review_state)
    setReviewDraftTouched(false)
    setWithholdingExemption('')
    setRedactionExemption('')
    setRedactionExplanation('')
    setDraftBox(null)
    setSelectedProposal(null)
    setProposalRejectReason('')
    setDragStart(null)
    setVoidTarget(null)
    setVoidReason('')
    const next = new URLSearchParams(searchParams)
    next.set('item', item.id)
    setSearchParams(next)
  }

  function closeReview() {
    setSelected(null)
    const next = new URLSearchParams(searchParams)
    next.delete('item')
    setSearchParams(next)
  }

  function updateListParams(changes: Record<string, string>) {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    // Any change to what is being listed invalidates the current offset.
    if (!('page' in changes)) next.delete('page')
    setSearchParams(next)
  }

  function changeReviewState(value: string) {
    // Through changeFilter, not updateListParams: the chips narrow the listed
    // set exactly as the other filters do, so they drop the selection for the
    // same reason.
    changeFilter({ record_state: value })
  }

  // Changing what is listed -- a filter, a chip, or a page -- changes what
  // "selected" could mean, so the selection is dropped rather than silently
  // carried onto rows the reviewer can no longer see. Paging counts: a
  // selection that survived it would put records the reviewer is no longer
  // looking at into a decision the confirmation calls theirs, and the
  // confirmation's own privilege count is read off the visible rows.
  function changeFilter(changes: Record<string, string>) {
    setBulkSelection([])
    setBulkState('')
    setMatchingMode(false)
    updateListParams(changes)
  }

  function toggleSelection(id: string) {
    setBulkSelection((current) => (
      current.includes(id)
        ? current.filter((value) => value !== id)
        : current.concat(id)
    ))
  }

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!canReview || selected?.review_state !== 'responsive') return
    const start = pointerPosition(event)
    setDragStart(start)
    setDraftBox(normalizedBox(start, start))
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragStart) return
    setDraftBox(normalizedBox(dragStart, pointerPosition(event)))
  }

  function onPointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragStart) return
    const box = normalizedBox(dragStart, pointerPosition(event))
    setDragStart(null)
    if (box.x1 - box.x0 < 0.005 || box.y1 - box.y0 < 0.005) {
      setDraftBox(null)
      toast('error', 'Draw a larger redaction region.')
      return
    }
    setDraftBox(box)
  }

  function reviewProposal(proposal: RedactionProposal) {
    setSelectedProposal(proposal.id)
    setProposalRejectReason('')
    setPageNumber(proposal.page_number)
    setDraftBox({
      x0: proposal.x0,
      y0: proposal.y0,
      x1: proposal.x1,
      y1: proposal.y1,
    })
    setRedactionExemption(String(proposal.exemption))
    setRedactionExplanation(proposal.rationale)
  }

  // Facet counts stay advisory labels on the filters. A missing facet (the
  // facets request is still loading, or failed) simply shows no count rather
  // than hiding the filter.
  function facetRows(key: 'tags' | 'sender_domains' | 'custodians'): FacetRow[] {
    const rows = facets.data?.[key]
    return Array.isArray(rows) ? rows : []
  }

  function tagCount(tag: string): string {
    const row = facetRows('tags').find((entry) => entry.value === tag)
    return row ? ` (${row.count})` : ''
  }

  function facetCount(key: 'privilege'): string {
    const value = facets.data?.[key]
    return typeof value === 'number' ? ` (${value})` : ''
  }

  // Keep the active value visible even when it falls outside the capped facet
  // list, so a filter never silently reads as "Any".
  function withActiveValue(rows: FacetRow[], active: string): FacetRow[] {
    if (!active || rows.some((row) => row.value === active)) return rows
    return [{ value: active, count: 0 }, ...rows]
  }

  const senderDomainOptions = withActiveValue(
    facetRows('sender_domains'), activeSenderDomain,
  )
  const custodianOptions = withActiveValue(
    facetRows('custodians'), activeCustodian,
  )

  const listRows = list.data ? unwrap(list.data) : []
  // The selection only ever means "these visible rows", and the filters are
  // not the only thing that changes them. Reviewing a record in the detail
  // panel invalidates ['record-items'], the row can drop out of the active
  // filter on the refetch, and nothing there goes through `changeFilter`. A
  // stale id left behind would be submitted with the batch and would be
  // missing from the privilege count, so the selection is pruned to what is
  // actually on screen every time the list comes back.
  useEffect(() => {
    const visible = new Set(
      // Eligible rows only: a record decided withheld or duplicate from the
      // single-record panel while it sat in the selection leaves it here.
      (list.data ? unwrap(list.data) : []).filter(bulkEligible).map((row) => row.id),
    )
    setBulkSelection((current) => {
      const kept = current.filter((id) => visible.has(id))
      // Same array when nothing was pruned, so this never re-renders on its
      // own output.
      return kept.length === current.length ? current : kept
    })
  }, [list.data])
  const selectedOnPage = listRows.filter((row) => bulkSelection.includes(row.id))
  // The whole selection, not part of it: the pruning above keeps every
  // selected id among these rows, so this counts the batch that will actually
  // be submitted. A count taken from part of the batch could say "0
  // privileged" about a batch that carried some, which is the one thing the
  // privilege queue exists to prevent.
  const privilegeSelectedCount = selectedOnPage.filter(
    (row) => asList(row.tags).some((tag) => PRIVILEGE_TAGS.includes(tag)),
  ).length
  // A record with two tags appears under both headings when grouping by tag,
  // which is the truthful answer: it really is in both groups.
  const groupedRows: [string, RecordItem[]][] = activeGroup
    ? Array.from(
      listRows.reduce((groups, row) => {
        for (const key of groupKeys(row, activeGroup)) {
          groups.set(key, (groups.get(key) ?? []).concat(row))
        }
        return groups
      }, new Map<string, RecordItem[]>()),
    ).sort(([left], [right]) => left.localeCompare(right))
    : []

  // Scoped to the rows it is rendered above, never to the page. When the queue
  // is grouped, each group renders its own table with its own header
  // checkbox; a page-wide toggle there would stage records from groups the
  // reviewer never opened for a decision the confirmation calls theirs.
  function toggleSelectAll(rows: RecordItem[], allSelected: boolean) {
    const ids = rows.filter(bulkEligible).map((row) => row.id)
    if (allSelected) {
      setBulkSelection(bulkSelection.filter((id) => !ids.includes(id)))
      return
    }
    // Not truncated to the cap here. A selection that quietly dropped records
    // would leave the reviewer believing one is in the decision when it is
    // not; an over-cap selection instead says so and disables the control.
    // It takes a page larger than the cap to get there at all, because the
    // selection only ever holds ids the current list returned.
    setBulkSelection(Array.from(new Set(bulkSelection.concat(ids))))
  }

  // Built per rendered row-set rather than once, so the select-all checkbox
  // above a group answers for that group and nothing else.
  function columnsFor(rows: RecordItem[], label: string): Column<RecordItem>[] {
    const eligible = rows.filter(bulkEligible)
    const allSelected = eligible.length > 0
      && eligible.every((row) => bulkSelection.includes(row.id))
    return [
    ...(canReview ? [{
      header: 'Select',
      headerCell: (
        <input
          type="checkbox"
          aria-label={label}
          checked={allSelected}
          disabled={eligible.length === 0}
          onChange={() => toggleSelectAll(rows, allSelected)}
        />
      ),
      cell: (row: RecordItem) => (
        <input
          type="checkbox"
          aria-label={`Select ${row.original_filename}`}
          checked={bulkSelection.includes(row.id)}
          disabled={!bulkEligible(row)}
          title={bulkEligible(row) ? undefined : ONE_AT_A_TIME}
          onClick={(event) => event.stopPropagation()}
          onChange={() => toggleSelection(row.id)}
        />
      ),
    } satisfies Column<RecordItem>] : []),
    { header: 'Filename', cell: (row) => <span className="break-all">{row.original_filename}</span> },
    {
      header: 'Tags',
      cell: (row) => asList(row.tags).length ? (
        <div className="flex flex-wrap gap-1">
          {TAG_ORDER.filter((tag) => asList(row.tags).includes(tag)).map((tag) => (
            <Badge key={tag} tone={TAG_TONE[tag] ?? 'neutral'}>
              {TAG_LABELS[tag] ?? tag}
            </Badge>
          ))}
        </div>
      ) : '—',
    },
    {
      header: 'Sender',
      // Only a Records Officer is served the correspondent addresses; for
      // everyone else the API blanks them, the same way it already blanks
      // requester identity. The sending domain is not identity and is what
      // triage actually reads, so the column falls back to it rather than
      // going empty for the roles that do most of the reviewing.
      cell: (row) => row.email_from ? (
        <span className="break-all" title={row.email_to || undefined}>
          {row.email_from}
        </span>
      ) : row.email_sender_domain ? (
        <span className="break-all text-muted-foreground">
          {row.email_sender_domain}
        </span>
      ) : '—',
    },
    { header: 'Source system', cell: (row) => row.source_system || '—' },
    {
      header: 'Custodians',
      // Every mailbox the message was found in. One message deduplicated
      // across several accounts is one record that still names all of them.
      cell: (row) => {
        const names = asList(row.custodians).length
          ? asList(row.custodians)
          : (row.custodian ? [row.custodian] : [])
        if (names.length === 0) return '—'
        return (
          <span title={names.join(', ')}>
            {names[0]}
            {names.length > 1 && (
              // /80, not /70: at this size text-ink-muted on white measures
              // 4.44:1, below the WCAG 2 AA 4.5:1 minimum.
              <span className="text-ink-muted"> +{names.length - 1} more</span>
            )}
          </span>
        )
      },
    },
    {
      header: 'AI suggestion',
      cell: (row) => row.advisory_suggested_state ? (
        <span title="Advisory only — a named human still decides.">
          {row.advisory_suggested_state.replace('_', ' ')}
          {typeof row.advisory_confidence === 'number' && (
            <span className="text-ink-muted">
              {' '}({row.advisory_confidence.toFixed(2)})
            </span>
          )}
        </span>
      ) : '—',
    },
    {
      header: 'Review state',
      cell: (row) => <Badge tone={reviewStateTone(row.review_state)}>{reviewStateLabel(row.review_state)}</Badge>,
    },
    {
      header: 'File type',
      cell: (row) => (
        <span title={`${row.content_type || 'unknown type'} — ${row.handling_detail}`}>
          <Badge tone={HANDLING_TONE[row.handling] ?? 'neutral'}>
            {row.handling_label}
          </Badge>
        </span>
      ),
    },
    { header: 'Size', cell: (row) => formatBytes(row.content_length), align: 'right' },
    { header: 'Redactions', cell: (row) => row.redaction_count, align: 'right' },
    {
      header: 'Exemption citations',
      cell: (row) => row.withholding_exemption_citations.length ? (
        <div className="flex flex-wrap gap-1">
          {row.withholding_exemption_citations.map((citation) => (
            <Badge key={citation} tone="neutral">{citation}</Badge>
          ))}
        </div>
      ) : '—',
    },
    {
      header: 'SHA-256',
      cell: (row) => (
        <span title={row.sha256} className="font-mono text-xs text-ink-muted">
          {truncateHash(row.sha256)}
        </span>
      ),
    },
    { header: 'Created', cell: (row) => row.created_at.slice(0, 10) },
    ]
  }

  const activeExemptions = exemptions.data ? unwrap(exemptions.data) : []
  const activeRedactions = redactions.data ? unwrap(redactions.data) : []
  const visibleRedactions = activeRedactions.filter((row) => row.page_number === pageNumber)
  const redactionProposals = redactionAdvisory.data?.proposals ?? []
  const undecidedProposals = redactionProposals.filter((proposal) => proposal.decision === null)
  const visibleProposals = undecidedProposals.filter(
    (proposal) => proposal.page_number === pageNumber && proposal.id !== selectedProposal,
  )
  // The envelope's count is the whole result set; the array is one page of it.
  const listCount = list.data && !Array.isArray(list.data)
    ? list.data.count
    : listRows.length
  const pageCount = metadata.data?.page_count ?? 0
  const secondReviewSatisfied = !selected?.second_review_required || Boolean(selected.second_reviewed_at)

  return (
    <>
      {/* One compact control row, matching the Inbox command bar. This was two
          labelled chip stacks and a labelled search row inside a card, which
          spent roughly 250px on controls before the first record appeared.
          Review state is the axis reviewers actually work along, so it stays
          as chips; file type is a narrower cut and becomes a select. Every
          filter is still reachable and still keyed to the same URL params. */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Review state filters" className="flex flex-wrap gap-2">
          <FilterChip
            selected={activeReviewState === ''}
            onClick={() => changeReviewState('')}
          >
            All
          </FilterChip>
          {REVIEW_STATES.map((state) => (
            <FilterChip
              key={state.value}
              selected={activeReviewState === state.value}
              onClick={() => changeReviewState(state.value)}
            >
              {state.label}
            </FilterChip>
          ))}
        </div>

        <Select
          aria-label="File type"
          className="w-auto"
          value={activeHandling}
          onChange={(event) => changeFilter({ handling: event.target.value })}
        >
          <option value="">Any file type</option>
          {HANDLING_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </Select>

        {/* Tags are facts read off the message headers. "Privilege queue" is
            the union of the two tags that mean a human must look before
            anything ships; it decides nothing by itself. */}
        <Select
          aria-label="Tag"
          className="w-auto"
          value={activeTag}
          onChange={(event) => changeFilter({ tag: event.target.value })}
        >
          <option value="">Any tag</option>
          <option value="privilege">
            Privilege queue{facetCount('privilege')}
          </option>
          {TAG_ORDER.map((tag) => (
            <option key={tag} value={tag}>
              {TAG_LABELS[tag]}{tagCount(tag)}
            </option>
          ))}
        </Select>

        <Select
          aria-label="Sender domain"
          className="w-auto"
          value={activeSenderDomain}
          onChange={(event) => changeFilter({ sender: event.target.value })}
        >
          <option value="">Any sender domain</option>
          {senderDomainOptions.map((row) => (
            <option key={row.value} value={row.value}>
              {row.value} ({row.count})
            </option>
          ))}
        </Select>

        <Select
          aria-label="Custodian"
          className="w-auto"
          value={activeCustodian}
          onChange={(event) => changeFilter({ custodian: event.target.value })}
        >
          <option value="">Any custodian</option>
          {custodianOptions.map((row) => (
            <option key={row.value} value={row.value}>
              {row.value} ({row.count})
            </option>
          ))}
        </Select>

        <Select
          aria-label="AI confidence"
          className="w-auto"
          value={activeConfidence}
          onChange={(event) => changeFilter({ confidence: event.target.value })}
        >
          {CONFIDENCE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </Select>

        <form
          className="flex min-w-[14rem] flex-1 items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            changeFilter({ q: searchDraft.trim() })
          }}
        >
          <Input
            type="search"
            aria-label="Search records"
            className="min-w-0 flex-1"
            value={searchDraft}
            placeholder={
              canSearchCorrespondents
                ? 'Search filenames, extracted text, and correspondents'
                : 'Search filenames and extracted text'
            }
            onChange={(event) => setSearchDraft(event.target.value)}
          />
          <Button type="submit">Search</Button>
          {activeSearch && (
            <Button
              variant="secondary"
              type="button"
              onClick={() => {
                setSearchDraft('')
                changeFilter({ q: '' })
              }}
            >
              Clear
            </Button>
          )}
        </form>

        <Select
          aria-label="Sort"
          className="w-auto"
          value={activeSort}
          onChange={(event) => updateListParams({ sort: event.target.value })}
        >
          {SORT_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </Select>

        <Select
          aria-label="Group by"
          className="w-auto"
          value={activeGroup}
          onChange={(event) => updateListParams({ group: event.target.value })}
        >
          {GROUP_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </Select>
      </div>

      {/* aria-live on the bar: the selection count and the cap notice change
          without anything moving focus, so a screen-reader user would
          otherwise get no feedback that a click changed what a confirmation
          covers. */}
      {canReview && hasFilter && !matchingMode && (facets.data?.total ?? 0) > 0 && (
        <div className="mb-2 flex justify-end">
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setBulkSelection([])
              setMatchingMode(true)
            }}
          >
            Select all {facets.data?.total} matching
          </Button>
        </div>
      )}

      {canReview && (bulkSelection.length > 0 || matchingMode) && (
        <div
          aria-live="polite"
          className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-sunken px-3 py-2"
        >
          <span className="text-sm text-ink">
            {matchingMode
              ? `All ${facets.data?.total ?? 0} records matching this filter`
              : `${bulkSelection.length} selected`}
          </span>
          <Select
            aria-label="Bulk review outcome"
            className="w-auto"
            value={bulkState}
            onChange={(event) => setBulkState(event.target.value)}
          >
            <option value="">Choose an outcome…</option>
            {BULK_REVIEW_STATES.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </Select>
          <Button
            type="button"
            disabled={
              bulkState === ''
              || bulkReviewMutation.isPending
              || bulkFilterMutation.isPending
              // The confirmation refuses an over-cap selection anyway, so
              // opening it would only be a dead end.
              || (!matchingMode && bulkSelection.length > BULK_REVIEW_MAX)
            }
            onClick={() => setBulkConfirmOpen(true)}
          >
            {matchingMode ? 'Review all matching' : 'Review selected'}
          </Button>
          <Button
            variant="secondary"
            type="button"
            onClick={() => {
              setBulkSelection([])
              setBulkState('')
              setMatchingMode(false)
            }}
          >
            Clear selection
          </Button>
          {!matchingMode && bulkSelection.length > BULK_REVIEW_MAX && (
            <span className="text-sm text-danger-ink">
              Select at most {BULK_REVIEW_MAX} records for one decision.
            </span>
          )}
        </div>
      )}

      {list.isPending && <Spinner />}
      {list.isError && <ErrorNote error={list.error} />}
      {targetedItem.isError && <ErrorNote error={targetedItem.error} />}
      {canReview && request !== '' && (
        <DiscardBatchCard request={request} onOpen={(id) => updateListParams({ item: id })} />
      )}
      {activeGroup === 'cluster' && request !== '' && (
        <RecordClusters
          filters={filterParams()}
          filterKey={filterKey}
          clusterBy={searchParams.get('cluster_by') ?? 'thread'}
          onClusterBy={(value) => updateListParams({ cluster_by: value })}
          onOpen={(id) => updateListParams({ item: id })}
        />
      )}
      {activeGroup === 'cluster' && request === '' && (
        <Card><EmptyState>Open a case to group its records into clusters.</EmptyState></Card>
      )}
      {list.isSuccess && activeGroup !== 'cluster' && (
        <Card>
          {listRows.length === 0 ? (
            <EmptyState>No record items match these filters.</EmptyState>
          ) : activeGroup ? (
            // Grouping is a presentation of this page's rows, not a second
            // query: the set shown is exactly the set the filters returned.
            <div className="space-y-4">
              {groupedRows.map(([label, rows]) => (
                <section key={label}>
                  <h3 className="mb-2 font-heading text-sm font-semibold text-ink">
                    {label} <span className="text-ink-muted">({rows.length})</span>
                  </h3>
                  <Table
                    columns={columnsFor(rows, `Select every record in ${label}`)}
                    rows={rows}
                    rowKey={(row) => row.id}
                    onRowClick={openReview}
                  />
                </section>
              ))}
            </div>
          ) : (
            <Table
              columns={columnsFor(listRows, 'Select every record on this page')}
              rows={listRows}
              rowKey={(row) => row.id}
              onRowClick={openReview}
            />
          )}
          <div className="mt-3">
            <Pager
              count={listCount}
              page={listPage}
              pageSize={RECORD_PAGE_SIZE}
              noun="file"
              onPage={(page) => changeFilter({ page: String(page) })}
            />
          </div>
        </Card>
      )}

      <Modal
        open={selected !== null}
        title={selected ? `${selected.original_filename} — ${selected.request_number}` : ''}
        onClose={closeReview}
        size="wide"
      >
        {selected && (
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1.5fr)_minmax(19rem,0.8fr)]">
            <section className="min-w-0 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap gap-2">
                  <Badge tone={reviewStateTone(selected.review_state)}>
                    {reviewStateLabel(selected.review_state)}
                  </Badge>
                  {selected.second_review_required && (
                    <Badge tone={selected.second_reviewed_at ? 'green' : 'amber'}>
                      {selected.second_reviewed_at ? 'Second review approved' : 'Second review required'}
                    </Badge>
                  )}
                  {selected.redacted_artifact_ready && <Badge tone="green">Redacted PDF ready</Badge>}
                </div>
                {pageCount > 0 && (
                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      disabled={pageNumber <= 1}
                      onClick={() => {
                        setPageNumber((page) => page - 1)
                        setDraftBox(null)
                        setSelectedProposal(null)
                      }}
                    >
                      Previous
                    </Button>
                    <span className="text-sm text-ink">Page {pageNumber} of {pageCount}</span>
                    <Button
                      variant="ghost"
                      disabled={pageNumber >= pageCount}
                      onClick={() => {
                        setPageNumber((page) => page + 1)
                        setDraftBox(null)
                        setSelectedProposal(null)
                      }}
                    >
                      Next
                    </Button>
                  </div>
                )}
              </div>

              {selected.reviewable === false && (
                <RecordHandlingPanel record={selected} canReview={canReview} />
              )}
              {selected.reviewable !== false && metadata.isPending && (
                <Spinner label="Verifying retained PDF…" />
              )}
              {selected.reviewable !== false && metadata.isError && (
                <ErrorNote error={metadata.error} />
              )}
              {selected.reviewable !== false && metadata.isSuccess && (
                <div
                  aria-label={`Draw redactions on page ${pageNumber}`}
                  className={`relative mx-auto w-fit max-w-full overflow-hidden rounded-md border border-line bg-surface-raised ${
                    canReview && selected.review_state === 'responsive' ? 'cursor-crosshair touch-none' : ''
                  }`}
                  onPointerDown={onPointerDown}
                  onPointerMove={onPointerMove}
                  onPointerUp={onPointerUp}
                  onPointerCancel={() => { setDragStart(null); setDraftBox(null) }}
                >
                  <img
                    draggable={false}
                    className="pointer-events-none block max-h-[68vh] max-w-full select-none"
                    src={`/api/staff/record-items/${selected.id}/preview-page/?page=${pageNumber}`}
                    alt={`Record page ${pageNumber} of ${pageCount}`}
                  />
                  {visibleRedactions.map((redaction) => (
                    <Region key={redaction.id} box={redaction} />
                  ))}
                  {visibleProposals.map((proposal) => (
                    <Region key={proposal.id} box={proposal} proposal />
                  ))}
                  {draftBox && <Region box={draftBox} draft />}
                </div>
              )}
              <p className="text-xs text-ink-muted">
                The browser receives only a no-store raster preview. Draw from one corner to the
                other; saved coordinates are normalized to the page. Blue regions are AI
                candidates, amber is the current human-editable draft, and black regions are
                approved redactions.
              </p>
            </section>

            <aside className="space-y-5">
              {canReview && (
                <section className="space-y-3 rounded-xl border border-warning bg-warning-soft p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h3 className="font-heading text-base font-bold text-ink">
                        AI responsiveness recommendation
                      </h3>
                      <p className="mt-1 text-xs text-ink-muted">
                        Prepared automatically for human judgment. The recommendation is
                        permanently non-applying and cannot choose the legal review state.
                      </p>
                    </div>
                    {responsivenessAdvisory.data?.latest?.state === 'failed' && (
                      <Button
                        variant="ghost"
                        disabled={responsivenessAdvisoryMutation.isPending}
                        onClick={() => responsivenessAdvisoryMutation.mutate()}
                      >
                        Retry AI analysis
                      </Button>
                    )}
                  </div>
                  {responsivenessAdvisory.isError && (
                    <ErrorNote error={responsivenessAdvisory.error} />
                  )}
                  {responsivenessAdvisory.data && !responsivenessAdvisory.data.enabled && (
                    <p className="text-sm text-ink-muted">
                      Disabled pending district provider and data-handling approval.
                    </p>
                  )}
                  {responsivenessAdvisory.data?.enabled
                    && responsivenessAdvisory.data.latest === null && (
                    <p className="text-sm text-ink-muted">
                      Automatic analysis is queued as soon as this record becomes review-ready.
                    </p>
                  )}
                  {['pending', 'running'].includes(
                    responsivenessAdvisory.data?.latest?.state ?? '',
                  ) && <p className="text-sm text-ink-muted">Analysis in progress…</p>}
                  {responsivenessAdvisory.data?.latest?.state === 'failed' && (
                    <p className="text-sm text-danger-ink">
                      {responsivenessAdvisory.data.latest.failure_detail}
                    </p>
                  )}
                  {responsivenessAdvisory.data?.latest?.state === 'complete' && (
                    <div className="space-y-2 text-sm text-ink">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone="amber">
                          Suggested: {responsivenessAdvisory.data.latest.proposal.suggested_state}
                        </Badge>
                        <span>
                          Confidence{' '}
                          {Math.round(
                            (responsivenessAdvisory.data.latest.proposal.confidence ?? 0) * 100,
                          )}%
                        </span>
                      </div>
                      <p>{responsivenessAdvisory.data.latest.proposal.rationale}</p>
                      <p className="text-xs text-ink-muted">
                        Matched terms:{' '}
                        {responsivenessAdvisory.data.latest.proposal.matched_terms?.join(' · ') || 'none'}
                      </p>
                      <p className="text-xs text-ink-muted">
                        Model {responsivenessAdvisory.data.latest.model_identifier} · input SHA-256{' '}
                        {responsivenessAdvisory.data.latest.input_sha256.slice(0, 16)}…
                        {responsivenessAdvisory.data.latest.input_truncated ? ' · input truncated' : ''}
                      </p>
                    </div>
                  )}
                </section>
              )}

              {canReview && (
                <section className="space-y-3 rounded-xl border border-line bg-surface-raised p-4">
                  <h3 className="font-heading text-base font-bold text-ink">Human review</h3>
                  <Field label="Review outcome">
                    <Select
                      className="w-full"
                      value={reviewDraft}
                      onChange={(event) => {
                        setReviewDraft(event.target.value)
                        setReviewDraftTouched(true)
                      }}
                    >
                      {HUMAN_REVIEW_STATES.map((state) => (
                        <option key={state.value} value={state.value}>{state.label}</option>
                      ))}
                    </Select>
                  </Field>
                  {reviewDraft === 'withheld' && (
                    <Field label="Withholding exemption">
                      <Select
                        className="w-full"
                        value={withholdingExemption}
                        onChange={(event) => setWithholdingExemption(event.target.value)}
                      >
                        <option value="">Select a legal basis</option>
                        {activeExemptions.map((exemption) => (
                          <option key={exemption.id} value={exemption.id}>
                            {exemption.citation} — {exemption.short_name}
                          </option>
                        ))}
                      </Select>
                    </Field>
                  )}
                  <Button
                    onClick={() => reviewMutation.mutate()}
                    disabled={
                      reviewMutation.isPending
                      || (reviewDraft === 'withheld' && !withholdingExemption)
                    }
                  >
                    Confirm review outcome
                  </Button>
                  <p className="text-xs text-ink-muted">
                    This is a named legal judgment. Automation may propose but cannot confirm it.
                  </p>
                </section>
              )}

              {canReview && selected.review_state === 'responsive' && (
                <section className="space-y-3 rounded-xl border border-info bg-info-soft p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h3 className="font-heading text-base font-bold text-ink">
                        AI redaction proposals
                      </h3>
                      <p className="mt-1 text-xs text-ink-muted">
                        Bedrock prepares grounded candidates and reasons automatically. Every
                        candidate remains unapplied until a named reviewer accepts an editable
                        region, legal citation, and explanation.
                      </p>
                    </div>
                    {redactionAdvisory.data?.latest?.state === 'failed' && (
                      <Button
                        variant="ghost"
                        disabled={redactionAdvisoryMutation.isPending}
                        onClick={() => redactionAdvisoryMutation.mutate()}
                      >
                        Retry AI redaction analysis
                      </Button>
                    )}
                  </div>
                  {redactionAdvisory.isPending && <Spinner label="Loading redaction proposals…" />}
                  {redactionAdvisory.isError && <ErrorNote error={redactionAdvisory.error} />}
                  {redactionAdvisory.data && !redactionAdvisory.data.enabled && (
                    <p className="text-sm text-ink-muted">AI advisory is disabled in this environment.</p>
                  )}
                  {redactionAdvisory.data?.enabled
                    && redactionAdvisory.data.latest === null && (
                    <p className="text-sm text-ink-muted">
                      Automatic redaction analysis is being prepared after the responsive decision.
                    </p>
                  )}
                  {['pending', 'running'].includes(
                    redactionAdvisory.data?.latest?.state ?? '',
                  ) && <p className="text-sm text-ink-muted">Redaction analysis in progress…</p>}
                  {redactionAdvisory.data?.latest?.state === 'failed' && (
                    <p className="text-sm text-danger-ink">
                      {redactionAdvisory.data.latest.failure_detail}
                    </p>
                  )}
                  {redactionAdvisory.data?.latest?.state === 'complete' && (
                    <>
                      <p className="text-sm font-semibold text-ink">
                        {undecidedProposals.length}{' '}
                        {undecidedProposals.length === 1 ? 'candidate needs' : 'candidates need'} review
                      </p>
                      {redactionProposals.length === 0 && (
                        <p className="text-sm text-ink-muted">
                          No redaction candidates were found. Human page review is still required.
                        </p>
                      )}
                      <div className="max-h-80 space-y-2 overflow-y-auto">
                        {redactionProposals.map((proposal) => (
                          <div
                            key={proposal.id}
                            className={`rounded-lg border p-3 text-sm ${
                              selectedProposal === proposal.id
                                ? 'border-warning bg-warning-soft'
                                : 'border-info bg-surface-raised'
                            }`}
                          >
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-heading font-bold text-ink">
                                Page {proposal.page_number} · {proposal.citation}
                              </span>
                              <Badge tone={
                                proposal.decision?.outcome === 'accepted'
                                  ? 'green'
                                  : proposal.decision?.outcome === 'rejected'
                                    ? 'neutral'
                                    : 'amber'
                              }>
                                {proposal.decision?.outcome ?? `${Math.round(proposal.confidence * 100)}% candidate`}
                              </Badge>
                            </div>
                            <p className="mt-1 text-ink-muted">{proposal.exemption_name}</p>
                            <p className="mt-1 text-ink">{proposal.rationale}</p>
                            {proposal.decision?.reason && (
                              <p className="mt-1 text-xs text-ink-muted">
                                Human reason: {proposal.decision.reason}
                              </p>
                            )}
                            {!proposal.decision && (
                              <Button
                                variant="ghost"
                                className="mt-2"
                                onClick={() => reviewProposal(proposal)}
                              >
                                Review proposal
                              </Button>
                            )}
                          </div>
                        ))}
                      </div>
                      {redactionAdvisory.data.latest && (
                        <p className="text-xs text-ink-muted">
                          Model {redactionAdvisory.data.latest.model_identifier} · input SHA-256{' '}
                          {redactionAdvisory.data.latest.input_sha256.slice(0, 16)}…
                        </p>
                      )}
                    </>
                  )}
                </section>
              )}

              {canReview && selected.review_state === 'responsive' && (
                <section className="space-y-3 rounded-xl border border-line bg-surface-raised p-4">
                  <h3 className="font-heading text-base font-bold text-ink">
                    {selectedProposal ? 'Review AI proposal' : 'Add redaction'}
                  </h3>
                  <p className="text-sm text-ink-muted">
                    {selectedProposal
                      ? 'Adjust the proposed box on the page, verify or change the legal exemption, then explicitly accept or reject it.'
                      : 'Draw a box on the page preview, then choose the legal exemption and add the redaction.'}
                  </p>
                  <Field label="Redaction exemption">
                    <Select
                      className="w-full"
                      value={redactionExemption}
                      onChange={(event) => setRedactionExemption(event.target.value)}
                    >
                      <option value="">Select a legal basis</option>
                      {activeExemptions.map((exemption) => (
                        <option key={exemption.id} value={exemption.id}>
                          {exemption.citation} — {exemption.short_name}
                          {exemption.requires_second_review ? ' (second review)' : ''}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Explanation" hint="Optional internal context; the citation remains the legal basis.">
                    <Textarea
                      rows={2}
                      value={redactionExplanation}
                      onChange={(event) => setRedactionExplanation(event.target.value)}
                    />
                  </Field>
                  {selectedProposal ? (
                    <div className="space-y-3">
                      <div className="flex flex-wrap gap-2">
                        <Button
                          onClick={() => acceptProposalMutation.mutate()}
                          disabled={
                            !draftBox
                            || !redactionExemption
                            || acceptProposalMutation.isPending
                          }
                        >
                          Accept edited proposal
                        </Button>
                        <Button
                          variant="ghost"
                          onClick={() => {
                            setSelectedProposal(null)
                            setProposalRejectReason('')
                            setDraftBox(null)
                            setRedactionExplanation('')
                          }}
                        >
                          Cancel proposal review
                        </Button>
                      </div>
                      <Field
                        label="Rejection reason"
                        hint={reasonHint('why this proposed redaction is wrong')}
                      >
                        <Textarea
                          rows={2}
                          minLength={MIN_REASON_LENGTH}
                          value={proposalRejectReason}
                          onChange={(event) => setProposalRejectReason(event.target.value)}
                        />
                      </Field>
                      {reasonShortfall(proposalRejectReason) && (
                        <p aria-live="polite" className="text-sm text-ink-muted">
                          {reasonShortfall(proposalRejectReason)}
                        </p>
                      )}
                      <Button
                        variant="danger"
                        disabled={
                          reasonTooShort(proposalRejectReason)
                          || rejectProposalMutation.isPending
                        }
                        onClick={() => rejectProposalMutation.mutate()}
                      >
                        Reject proposal
                      </Button>
                    </div>
                  ) : (
                    <Button
                      onClick={() => createRedactionMutation.mutate()}
                      disabled={!draftBox || !redactionExemption || createRedactionMutation.isPending}
                    >
                      Add redaction
                    </Button>
                  )}
                </section>
              )}

              {canReview && selected.review_state !== 'responsive' && (
                <section className="space-y-2 rounded-xl border border-line bg-surface-raised p-4">
                  <h3 className="font-heading text-base font-bold text-ink">Redaction workflow</h3>
                  <p className="text-sm text-ink-muted">
                    Redaction tools are available only after a record is confirmed Responsive. If
                    this record includes content that must be disclosed with protected information
                    removed, confirm Responsive above, draw a box on the page preview, choose the
                    exemption, and add the redaction.
                  </p>
                </section>
              )}

              <section className="space-y-3 rounded-xl border border-line bg-surface-raised p-4">
                <h3 className="font-heading text-base font-bold text-ink">Active redactions</h3>
                {redactions.isPending && <Spinner label="Loading redactions…" />}
                {redactions.isError && <ErrorNote error={redactions.error} />}
                {redactions.isSuccess && activeRedactions.length === 0 && (
                  <p className="text-sm text-ink-muted">No active redactions.</p>
                )}
                {activeRedactions.map((redaction) => (
                  <div key={redaction.id} className="rounded-lg border border-line p-3 text-sm">
                    <p className="font-heading font-bold text-ink">
                      Page {redaction.page_number} · {redaction.citation}
                    </p>
                    <p className="text-ink-muted">{redaction.exemption_name}</p>
                    {redaction.explanation && <p className="mt-1 text-ink">{redaction.explanation}</p>}
                    {canReview && (
                      <Button
                        variant="ghost"
                        className="mt-2"
                        onClick={() => { setVoidTarget(redaction.id); setVoidReason('') }}
                      >
                        Void redaction
                      </Button>
                    )}
                  </div>
                ))}
                {voidTarget !== null && (
                  <div className="space-y-2 rounded-lg border border-danger bg-danger-soft p-3">
                    <Field
                      label="Reason for voiding"
                      hint={reasonHint('this redaction stays in the exemption log')}
                    >
                      <Textarea
                        rows={2}
                        minLength={MIN_REASON_LENGTH}
                        value={voidReason}
                        onChange={(event) => setVoidReason(event.target.value)}
                      />
                    </Field>
                    {reasonShortfall(voidReason) && (
                      <p aria-live="polite" className="text-sm text-ink-muted">
                        {reasonShortfall(voidReason)}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button
                        variant="danger"
                        disabled={reasonTooShort(voidReason) || voidMutation.isPending}
                        onClick={() => voidMutation.mutate()}
                      >
                        Confirm void
                      </Button>
                      <Button variant="ghost" onClick={() => setVoidTarget(null)}>Cancel</Button>
                    </div>
                  </div>
                )}
              </section>

              {canReview && selected.second_review_required && !selected.second_reviewed_at && (
                <Button
                  variant="secondary"
                  onClick={() => secondReviewMutation.mutate()}
                  disabled={secondReviewMutation.isPending}
                >
                  Approve second review
                </Button>
              )}
              {canReview && activeRedactions.length > 0 && secondReviewSatisfied && (
                <Button
                  onClick={() => renderMutation.mutate()}
                  disabled={renderMutation.isPending}
                >
                  Build redacted PDF
                </Button>
              )}

              <ErrorNote
                error={
                  reviewMutation.error
                  || createRedactionMutation.error
                  || redactionAdvisoryMutation.error
                  || acceptProposalMutation.error
                  || rejectProposalMutation.error
                  || secondReviewMutation.error
                  || renderMutation.error
                  || voidMutation.error
                }
              />
            </aside>
          </div>
        )}
      </Modal>

      <Modal
        open={bulkConfirmOpen}
        title="Record this review outcome"
        onClose={() => setBulkConfirmOpen(false)}
      >
        {matchingMode ? (
          <div className="space-y-4">
            {selectionPreview.isPending && <Spinner />}
            {selectionPreview.isError && <ErrorNote error={selectionPreview.error} />}
            {selectionPreview.data && (
              <>
                <p className="text-sm text-ink">
                  You are recording{' '}
                  <strong>
                    {BULK_REVIEW_STATES.find((option) => option.value === bulkState)?.label
                      ?? bulkState}
                  </strong>{' '}
                  for <strong>{selectionPreview.data.eligible_count}</strong> of the{' '}
                  {selectionPreview.data.matching} records matching this filter. This is
                  your decision, not an acceptance of any AI suggestion. Each record is
                  audited separately in your name.
                </p>
                {Object.entries(selectionPreview.data.excluded)
                  .filter(([, count]) => count > 0)
                  .map(([key, count]) => (
                    <p key={key} className="text-sm text-ink-muted">
                      {count}{' '}
                      {EXCLUDED_LABELS[key as keyof BulkSelectionPreview['excluded']][
                        count === 1 ? 0 : 1
                      ]}{' '}
                      and will not change.
                    </p>
                  ))}
                {selectionPreview.data.privilege_count > 0 && (
                  <p className="text-sm font-semibold text-danger-ink">
                    {selectionPreview.data.privilege_count}{' '}
                    {selectionPreview.data.privilege_count === 1
                      ? 'record carries'
                      : 'records carry'}{' '}
                    a district-counsel or privilege marker. Filter to the privilege queue
                    and read them before deciding.
                  </p>
                )}
                {selectionPreview.data.eligible_count > selectionPreview.data.max_items && (
                  <p className="text-sm text-danger-ink">
                    One decision covers at most {selectionPreview.data.max_items} records.
                    Narrow the filter.
                  </p>
                )}
              </>
            )}
            <ErrorNote error={bulkFilterMutation.error} />
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                disabled={
                  !selectionPreview.data
                  || bulkFilterMutation.isPending
                  || selectionPreview.data.eligible_count === 0
                  || selectionPreview.data.eligible_count > selectionPreview.data.max_items
                }
                onClick={() => {
                  if (selectionPreview.data) bulkFilterMutation.mutate(selectionPreview.data)
                }}
              >
                Record {selectionPreview.data?.eligible_count ?? 0} outcomes
              </Button>
              <Button
                variant="secondary"
                type="button"
                onClick={() => setBulkConfirmOpen(false)}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : (
        <div className="space-y-4">
          <p className="text-sm text-ink">
            You are recording{' '}
            <strong>
              {BULK_REVIEW_STATES.find((option) => option.value === bulkState)?.label
                ?? bulkState}
            </strong>{' '}
            for <strong>{bulkSelection.length}</strong>{' '}
            {bulkSelection.length === 1 ? 'record' : 'records'} you selected.
            This is your decision on the records shown, not an acceptance of
            any AI suggestion. Each record is audited separately in your name.
          </p>
          {privilegeSelectedCount > 0 && (
            // The privilege queue exists so a human reads these before
            // anything ships. Nothing is blocked here -- a counsel-domain
            // record is not automatically exempt, and only a named reviewer
            // with a citation may withhold one -- but the reviewer is told
            // what is in front of them rather than deciding blind.
            <p className="text-sm font-semibold text-danger-ink">
              {privilegeSelectedCount}{' '}
              {privilegeSelectedCount === 1 ? 'record carries' : 'records carry'}{' '}
              a district-counsel or privilege marker. Read{' '}
              {privilegeSelectedCount === 1 ? 'it' : 'them'} before deciding:
              withholding one in full needs its own exemption citation and a
              second review.
            </p>
          )}
          <p className="text-sm text-ink-muted">
            Withholding a record in full, and marking one a duplicate, stay
            one-at-a-time decisions: each needs its own exemption citation or
            its own judgement. A record already in either state is left out of
            a bulk selection.
          </p>
          <ErrorNote error={bulkReviewMutation.error} />
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              disabled={
                bulkReviewMutation.isPending
                || bulkState === ''
                || bulkSelection.length === 0
                || bulkSelection.length > BULK_REVIEW_MAX
              }
              onClick={() => {
                bulkReviewMutation.mutate(
                  { ids: bulkSelection, state: bulkState },
                  { onSuccess: () => setBulkConfirmOpen(false) },
                )
              }}
            >
              Record {bulkSelection.length}{' '}
              {bulkSelection.length === 1 ? 'outcome' : 'outcomes'}
            </Button>
            <Button
              variant="secondary"
              type="button"
              onClick={() => setBulkConfirmOpen(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
        )}
      </Modal>
    </>
  )
}
