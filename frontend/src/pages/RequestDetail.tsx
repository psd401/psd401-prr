// ABOUTME: Request detail screen — legal record, deadlines, estimate history,
// ABOUTME: status timeline, collection tasks, and any footage preservation hold.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import type { StaffRole } from '../api/auth'
import { isTerminalStatus } from '../caseStatus'
import { AddToExistingCaseModal } from '../case/AddToExistingCaseModal'
import { IntakeClassificationCard } from '../case/IntakeClassificationCard'
import { INTAKE_LABEL_TEXT, intakeAction, type IntakeLabel } from '../case/intakeLabels'
import { CaseActivityFeed } from '../case/CaseActivityFeed'
import { CaseNotesPanel } from '../case/CaseNotesPanel'
import { VaultSourceCounts } from '../case/VaultSourceCounts'
import { countHeadline, type VaultSource, type VaultSourceCount } from '../vaultSources'
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
  reasonHint,
  reasonTooShort,
} from '../ui'
import { proposedVaultQuery } from '../vaultQuery'
import { formatDay } from '../date'

interface RequesterInfo {
  name: string
  email: string
  phone: string
  mailing_address: string
  city: string
  state: string
  postal_code: string
  company: string
}

interface IntakeAttachment {
  id: string
  original_filename: string
  content_type: string
  size: number
  sha256: string
  scan_status: 'pending' | 'clean' | 'infected' | 'error'
  created_at: string
  scanned_at: string | null
}

interface StatusEvent {
  occurred_at: string
  from_status: string
  to_status: string
  note: string
  actor: string | null
  visible_to_requester: boolean
}

interface EstimateRevision {
  previous_date: string | null
  new_date: string
  reason: string
  revised_by: string | null
  automated: boolean
  created_at: string
}

interface CollectionTask {
  id: number
  system: string | null
  state: string
  due_date: string | null
  kind: string
}

interface FootagePreservation {
  id: number
  state: string
}

interface RequestDetail {
  id: number
  number: string
  status: string
  /** Which law governs this matter: public_records, litigation_discovery, or ferpa_parent. */
  matter_kind: string
  matter_kind_display: string
  received_at: string
  statutory_response_due: string | null
  /** 34 CFR 99.10(b), 45 calendar days. Only a FERPA matter has one. */
  ferpa_access_due: string | null
  estimated_completion: string | null
  closed_at: string | null
  requester_email: string
  requester_name: string
  assigned_officer_username: string | null
  assigned_officer_id: number | null
  tolled_business_days: number
  acknowledgment_sent_at: string | null
  initial_response_delivered: boolean
  description: string
  channel: string
  requester: RequesterInfo | null
  events: StatusEvent[]
  estimate_revisions: EstimateRevision[]
  collection_tasks: CollectionTask[]
  footage_preservations: FootagePreservation[]
  intake_attachments: IntakeAttachment[]
  email_intake_auto_created: boolean
  email_intake_receipt_id: string | null
  /** The case of record this duplicate was merged into, when it was. */
  merged_into?: string | null
  /** Duplicates merged into this case, oldest first. */
  merged_sources?: string[]
  /** Who verified the FERPA requester's identity, when, and how. */
  ferpa_identity_verification?: {
    verified_at: string
    verified_by: string | null
    method: string
    method_display: string
    /** Null for a reader who is not a Records Officer; the fact is not. */
    note: string | null
  } | null
  /** The officer's statement that the first response went out elsewhere. */
  external_initial_response?: {
    sent_via: string
    sent_via_label: string
    responded_on: string | null
    evidence: string
    attested_by: string | null
    created_at: string
  } | null
  initial_response_plan: {
    state: string
    due_date: string | null
    automatic_check: string
    correspondence_id: string | null
    correspondence_status: string | null
    delivery_status: string | null
    automated: boolean | null
    next_action: string
    steps: {
      key: string
      label: string
      state: 'done' | 'current' | 'pending'
    }[]
  }
}

interface ResponsePlanCopy {
  title: string
  description: string
  tone: 'neutral' | 'green' | 'amber' | 'red' | 'blue'
}

interface StaffUserChoice {
  id: number
  username: string
  display_name: string
}

interface AdvisoryRun {
  id: string
  state: 'pending' | 'running' | 'complete' | 'failed'
  model_identifier: string
  input_sha256: string
  input_truncated: boolean
  applied: false
  proposal: {
    summary?: string
    required_search_terms?: string[]
    search_terms?: string[]
    record_system_slugs?: string[]
    clarification_questions?: string[]
    complexity_flags?: string[]
    date_range?: { start: string | null; end: string | null }
    custodians?: string[]
    narrowing_options?: { terms: string[]; rationale: string }[]
  }
  failure_detail: string
}

interface AdvisoryLatest {
  enabled: boolean
  latest: AdvisoryRun | null
}

interface VaultCountRun {
  id: string
  state: 'pending' | 'running' | 'complete' | 'failed'
  corpus: 'MAIL'
  /** Gmail, Drive, Chat; a count from before Drive and Chat is Gmail only. */
  sources?: VaultSource[]
  source_counts?: VaultSourceCount[]
  /** Whether a collection may start from this count (any source to export). */
  collectable?: boolean
  search_scope: 'accounts' | 'entire_org'
  terms: string
  custodian_emails: string[]
  date_range_start: string | null
  date_range_end: string | null
  input_sha256: string
  trigger: 'manual' | 'automatic'
  requested_by_username: string | null
  collection_task_id: number | null
  collection_task_state: string | null
  collection_task_retryable: boolean
  total_count: number | null
  queried_accounts_count: number | null
  matching_accounts_count: number | null
  non_queryable_accounts_count: number | null
  accounts_with_errors_count: number | null
  failure_code: string
  failure_detail: string
  created_at: string
  started_at: string | null
  completed_at: string | null
}

interface VaultCountLatest {
  enabled: boolean
  corpus: 'MAIL'
  sources?: VaultSource[]
  latest: VaultCountRun | null
}


/**
 * Where "Change status" may move a working case.
 *
 * Withdrawal and abandonment are deliberately absent: they end the case
 * rather than move it, they are open to every staff member rather than the
 * Records Officer alone, and they demand a written reason. They live on their
 * own "Close case…" action, which says all of that in its own name.
 */
const MANUAL_STATUS_CHOICES = [
  { value: 'received', label: 'Received' },
  { value: 'gathering', label: 'Gathering Records' },
  { value: 'review', label: 'Review & Redaction' },
  { value: 'installment', label: 'Producing Installments' },
  { value: 'fulfilled', label: 'Fulfilled' },
]

const CASE_CLOSURE_CHOICES = [
  { value: 'closed_withdrawn', label: 'Closed – Withdrawn' },
  { value: 'closed_abandoned', label: 'Closed – Abandoned' },
]

const SUBSTANTIVE_REASON_STATUSES = new Set(['fulfilled'])

/** Every role that works cases here, and so may record a closure. */
const CASE_WORK_ROLES: StaffRole[] = [
  'records_officer',
  'liaison',
  'reviewer',
  'admin',
]

const CHANNEL_LABELS: Record<string, string> = {
  portal: 'Online Portal',
  email: 'Email',
  mail: 'US Mail',
  in_person: 'In Person',
  phone: 'Phone',
}

const TASK_KIND_LABELS: Record<string, string> = {
  vault_mail: 'Vault – Gmail',
  vault_drive: 'Vault – Drive',
  vault_chat: 'Vault – Chat',
  manual: 'Manual / Department',
}

const TASK_STATE_TONES: Record<string, 'neutral' | 'blue' | 'green' | 'red'> = {
  pending: 'neutral',
  running: 'blue',
  ingesting: 'blue',
  complete: 'green',
  failed: 'red',
}

const TASK_STATE_LABELS: Record<string, string> = {
  pending: 'Pending',
  running: 'Running',
  ingesting: 'Ingesting export',
  complete: 'Complete',
  failed: 'Failed',
}

const PRESERVATION_STATE_LABELS: Record<string, string> = {
  pending: 'Pending — hold executing',
  held: 'Held',
  partial: 'Held (some cameras failed)',
  manual: 'Manual hold required',
  expired: 'Window outside retention',
  released: 'Released',
}

const ACTIVE_PRESERVATION_STATES = new Set(['pending', 'partial', 'manual'])

const ATTACHMENT_STATUS: Record<
  IntakeAttachment['scan_status'],
  { label: string; tone: 'neutral' | 'green' | 'red' | 'amber' }
> = {
  pending: { label: 'Scanning', tone: 'neutral' },
  clean: { label: 'Clean', tone: 'green' },
  infected: { label: 'Blocked — malware detected', tone: 'red' },
  error: { label: 'Scan unavailable', tone: 'amber' },
}

function humanFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Past this, a request opens folded: a forwarded thread can run for pages. */
const FOLDED_REQUEST_LINES = 12

/**
 * The request as received, folded to its first lines when it is long. The
 * whole text is one click away and is never altered (rule 9).
 */
function RequestText({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const long = text.split('\n').length > FOLDED_REQUEST_LINES || text.length > 1200
  return (
    <div>
      <div
        className={`whitespace-pre-wrap rounded-md border border-line bg-surface-sunken p-3 text-sm text-ink ${
          long && !open ? 'max-h-64 overflow-hidden [mask-image:linear-gradient(to_bottom,black_70%,transparent)]' : ''
        }`}
      >
        {text}
      </div>
      {long && (
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="mt-2 text-sm font-semibold text-accent underline-offset-2 hover:underline"
        >
          {open ? 'Show less' : 'Show full request'}
        </button>
      )}
    </div>
  )
}

export function RequestDetail({
  roles,
  mode,
}: {
  roles: StaffRole[]
  /** Which stage of the rail (#45) this mounts for: Intake is the overview. */
  mode: 'overview' | 'scope' | 'collection' | 'close'
}) {
  const { number } = useParams<{ number: string }>()
  const canManageRequest = roles.includes('records_officer')
  const canCloseCase = roles.some((role) => CASE_WORK_ROLES.includes(role))
  const showOverview = mode === 'overview'
  const showScope = mode === 'scope'
  const showCollection = mode === 'collection'
  // The Close stage carries the same lifecycle actions as Intake, so the
  // closing decision sits where the closure readiness report is read.
  const showClose = mode === 'close'
  const qc = useQueryClient()
  const toast = useToast()
  const navigate = useNavigate()

  const [transitioning, setTransitioning] = useState(false)
  const [reopening, setReopening] = useState(false)
  const [reopenStatus, setReopenStatus] = useState('gathering')
  const [reopenReason, setReopenReason] = useState('')
  const [toStatus, setToStatus] = useState('')
  const [note, setNote] = useState('')
  const [closingNonRequest, setClosingNonRequest] = useState(false)
  const [nonRequestReason, setNonRequestReason] = useState('')
  const [closingCase, setClosingCase] = useState(false)
  const [closeStatus, setCloseStatus] = useState('')
  const [closeReason, setCloseReason] = useState('')
  const [addingToCase, setAddingToCase] = useState(false)
  const [addToCaseDestination, setAddToCaseDestination] = useState('')
  const [merging, setMerging] = useState(false)
  const [mergeTarget, setMergeTarget] = useState('')
  const [mergeReason, setMergeReason] = useState('')
  const [verifyingFerpa, setVerifyingFerpa] = useState(false)
  const [ferpaMethod, setFerpaMethod] = useState('in_person_photo_id')
  const [ferpaNote, setFerpaNote] = useState('')
  const [attesting, setAttesting] = useState(false)
  const [attestVia, setAttestVia] = useState('nextrequest')
  const [attestDate, setAttestDate] = useState('')
  const [attestEvidence, setAttestEvidence] = useState('')
  const [vaultTerms, setVaultTerms] = useState('')
  const [vaultSearchScope, setVaultSearchScope] = useState<'accounts' | 'entire_org'>('accounts')
  const [vaultCustodians, setVaultCustodians] = useState('')
  const [vaultStartDate, setVaultStartDate] = useState('')
  const [vaultEndDate, setVaultEndDate] = useState('')
  // Fields the officer has changed by hand. Defaults (the AI reading, the
  // receipt date) fill only untouched fields, so an edit -- including
  // clearing a field -- is never overwritten when a reading arrives later.
  const vaultTouched = useRef({ start: false, end: false, custodians: false })
  const [vaultCollectionOpen, setVaultCollectionOpen] = useState(false)
  const [vaultCollectionConfirmed, setVaultCollectionConfirmed] = useState(false)
  const [vaultCollectionState, setVaultCollectionState] = useState<string | null>(null)
  const [vaultCollectionRetryable, setVaultCollectionRetryable] = useState(false)

  const detail = useQuery({
    queryKey: ['request', number],
    queryFn: () => api<RequestDetail>(`/api/staff/requests/${number}/`),
    enabled: !!number,
  })

  const ownerChoices = useQuery({
    queryKey: ['staff-users', 'records-officer'],
    queryFn: () => api<Page<StaffUserChoice> | StaffUserChoice[]>(
      '/api/staff/staff-users/?role=records_officer',
    ),
    enabled: !!number && canManageRequest && showOverview,
  })

  const intentAdvisory = useQuery({
    queryKey: ['request-intent-advisory', number],
    queryFn: () => api<AdvisoryLatest>(
      `/api/staff/requests/${number}/advisory-intent/`,
    ),
    enabled: !!number && canManageRequest && showScope,
    refetchInterval: (query) => {
      const state = query.state.data?.latest?.state
      return state === 'pending' || state === 'running' ? 2000 : false
    },
  })

  const vaultCounts = useQuery({
    queryKey: ['vault-counts', number],
    queryFn: () => api<VaultCountLatest>(
      `/api/staff/requests/${number}/vault-counts/`,
    ),
    enabled: !!number && canManageRequest && showScope,
    refetchInterval: (query) => {
      const state = query.state.data?.latest?.state
      return state === 'pending' || state === 'running' ? 2000 : false
    },
  })

  const latestIntentAdvisory = intentAdvisory.data?.latest
  const latestVaultCount = vaultCounts.data?.latest

  useEffect(() => {
    if (latestVaultCount) {
      setVaultTerms(latestVaultCount.terms)
      setVaultSearchScope(latestVaultCount.search_scope)
      setVaultCustodians(latestVaultCount.custodian_emails.join('\n'))
      setVaultStartDate(latestVaultCount.date_range_start ?? '')
      setVaultEndDate(latestVaultCount.date_range_end ?? '')
      setVaultCollectionState(latestVaultCount.collection_task_state)
      setVaultCollectionRetryable(latestVaultCount.collection_task_retryable)
      return
    }
    if (latestIntentAdvisory?.state === 'complete') {
      const proposal = latestIntentAdvisory.proposal
      setVaultTerms(
        (current) => current
          || proposedVaultQuery(proposal.required_search_terms, proposal.search_terms),
      )
      // The AI's reading also names a date range and the accounts to search;
      // they fill only empty fields, so an officer's edit is never undone.
      const start = proposal.date_range?.start
      if (start && !vaultTouched.current.start) setVaultStartDate(start)
      const custodians = proposal.custodians ?? []
      if (custodians.length > 0 && !vaultTouched.current.custodians) {
        setVaultCustodians(custodians.join('\n'))
      }
    }
  }, [latestIntentAdvisory, latestVaultCount])

  // Responsive records are those in existence when the request was received,
  // so the receipt date is the end date the officer would otherwise retype on
  // every case. It is a starting point, not a rule the server enforces: a
  // recorded count still wins, and the effect fills only a field the officer
  // has not touched, so an edit -- including clearing the field -- is never
  // overwritten afterwards.
  const receiptDate = detail.data?.received_at.slice(0, 10) ?? ''
  // The AI's proposed end date wins when it is earlier; nothing after receipt
  // can be responsive, so a later one is held to the receipt date.
  const proposedEnd = latestIntentAdvisory?.state === 'complete'
    ? latestIntentAdvisory.proposal.date_range?.end ?? ''
    : ''
  useEffect(() => {
    if (latestVaultCount || !receiptDate || vaultTouched.current.end) return
    setVaultEndDate(proposedEnd && proposedEnd < receiptDate ? proposedEnd : receiptDate)
  }, [receiptDate, latestVaultCount, proposedEnd])

  const requestIntentAdvisory = useMutation({
    mutationFn: () => api<AdvisoryRun>(
      `/api/staff/requests/${number}/advisory-intent/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['request-intent-advisory', number] })
      toast('success', 'Advisory analysis queued. No workflow fields were changed.')
    },
    onError: (error) => toast('error', error.message),
  })

  const requestVaultCount = useMutation({
    mutationFn: () => api<VaultCountRun>(
      `/api/staff/requests/${number}/vault-counts/`,
      {
        method: 'POST',
        body: {
          terms: vaultTerms.trim(),
          search_scope: vaultSearchScope,
          custodian_emails: vaultSearchScope === 'entire_org'
            ? []
            : vaultCustodians
              .split(/[\s,;]+/)
              .map((email) => email.trim())
              .filter(Boolean),
          date_range_start: vaultStartDate || null,
          date_range_end: vaultEndDate || null,
        },
      },
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['vault-counts', number] })
      toast('success', 'Vault count queued. No export, hold, or workflow change was created.')
    },
    onError: (error) => toast('error', error.message),
  })

  const startVaultCollection = useMutation({
    mutationFn: () => api<CollectionTask>(
      `/api/staff/requests/${number}/vault-collection/`,
      {
        method: 'POST',
        body: {
          count_run_id: latestVaultCount?.id,
          expected_input_sha256: latestVaultCount?.input_sha256,
          confirmed: true,
        },
      },
    ),
    onSuccess: () => {
      setVaultCollectionOpen(false)
      setVaultCollectionConfirmed(false)
      setVaultCollectionState('pending')
      setVaultCollectionRetryable(false)
      qc.invalidateQueries({ queryKey: ['request', number] })
      qc.invalidateQueries({ queryKey: ['vault-counts', number] })
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
      toast('success', 'Collection task created. Track it under Work, then review imported records.')
    },
    onError: (error) => toast('error', error.message),
  })

  const transition = useMutation({
    mutationFn: (payload: { to_status: string; note: string }) =>
      api<RequestDetail>(`/api/staff/requests/${number}/transition/`, {
        method: 'POST',
        body: payload,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['request', number] })
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
      toast('success', 'Status updated.')
      closeTransition()
    },
  })

  const assignOwner = useMutation({
    mutationFn: (assignedOfficerId: number | null) =>
      api<RequestDetail>(`/api/staff/requests/${number}/`, {
        method: 'PATCH',
        body: { assigned_officer_id: assignedOfficerId },
      }),
    onSuccess: (updated) => {
      qc.setQueryData(['request', number], updated)
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
      toast('success', 'Case owner updated.')
    },
    onError: (error) => toast('error', error.message),
  })

  const closeNonRequest = useMutation({
    mutationFn: (payload: { reason: string }) =>
      api<RequestDetail>(`/api/staff/requests/${number}/close-non-request/`, {
        method: 'POST',
        body: payload,
      }),
    onSuccess: (updated) => {
      qc.setQueryData(['request', number], updated)
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
      toast('success', 'Request closed as a non-request.')
      setClosingNonRequest(false)
      setNonRequestReason('')
    },
  })

  const closeCase = useMutation({
    mutationFn: (payload: { to_status: string; reason: string }) =>
      api<RequestDetail>(`/api/staff/requests/${number}/close/`, {
        method: 'POST',
        body: payload,
      }),
    onSuccess: (updated) => {
      qc.setQueryData(['request', number], updated)
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
      qc.invalidateQueries({ queryKey: ['case-activity', number] })
      toast('success', 'Case closed. The reason is on the timeline.')
      setClosingCase(false)
      setCloseReason('')
    },
  })

  const downloadAttachment = useMutation({
    mutationFn: (id: string) =>
      api<{ url: string; expires_in: number }>(`/api/staff/intake-attachments/${id}/download/`, {
        method: 'POST',
      }),
    onSuccess: ({ url }) => {
      window.location.assign(url)
    },
  })

  // 34 CFR 99.10 gives the right of access to a parent or eligible student
  // and to nobody else. This records the officer's judgment that the person
  // asking is that person. It discloses nothing.
  const verifyFerpaIdentity = useMutation({
    mutationFn: () => api<RequestDetail>(
      `/api/staff/requests/${number}/ferpa-identity-verification/`,
      {
        method: 'POST',
        body: { method: ferpaMethod, note: ferpaNote.trim() },
      },
    ),
    onSuccess: async () => {
      setVerifyingFerpa(false)
      setFerpaNote('')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['request', number] }),
        qc.invalidateQueries({ queryKey: ['case-activity', number] }),
      ])
      toast('success', 'Identity verification recorded under your name.')
    },
    onError: (error) => toast('error', error.message),
  })

  // The officer's written statement that the five-day response already went
  // out through a channel this system never delivered. Sends nothing.
  const attestResponse = useMutation({
    mutationFn: () => api<RequestDetail>(
      `/api/staff/requests/${number}/external-initial-response/`,
      {
        method: 'POST',
        body: {
          sent_via: attestVia,
          responded_on: attestDate || null,
          evidence: attestEvidence.trim(),
        },
      },
    ),
    onSuccess: async () => {
      setAttesting(false)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['request', number] }),
        qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] }),
        qc.invalidateQueries({ queryKey: ['case-activity', number] }),
      ])
      toast('success', 'First response recorded on your statement.')
    },
    onError: (error) => toast('error', error.message),
  })

  // A merge ends on the case of record: that is where the officer's next
  // action is, and where the requester's emails now are.
  const merge = useMutation({
    mutationFn: () => api<RequestDetail>(`/api/staff/requests/${number}/merge/`, {
      method: 'POST',
      body: {
        target_number: mergeTarget.trim().toUpperCase(),
        reason: mergeReason.trim(),
      },
    }),
    onSuccess: async (target) => {
      setMerging(false)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['request'] }),
        qc.invalidateQueries({ queryKey: ['case-workspace-summary'] }),
        qc.invalidateQueries({ queryKey: ['case-activity'] }),
        qc.invalidateQueries({ queryKey: ['inbox-cases'] }),
      ])
      toast('success', `${number} merged into ${target.number}.`)
      navigate(`/requests/${target.number}`)
    },
    onError: (error) => toast('error', error.message),
  })

  const reopen = useMutation({
    mutationFn: () => api(`/api/staff/requests/${number}/reopen/`, {
      method: 'POST',
      body: { to_status: reopenStatus, reason: reopenReason },
    }),
    onSuccess: async () => {
      setReopening(false)
      setReopenReason('')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['request', number] }),
        qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] }),
        qc.invalidateQueries({ queryKey: ['case-activity', number] }),
      ])
      toast('success', 'Request reopened. The closure stays on the timeline.')
    },
    onError: (error) => toast('error', error.message),
  })

  function openTransition() {
    setToStatus('')
    setNote('')
    transition.reset()
    setTransitioning(true)
  }

  function closeTransition() {
    setTransitioning(false)
    transition.reset()
  }

  function submitTransition(e: FormEvent) {
    e.preventDefault()
    if (!toStatus) return
    const requiresSubstantiveReason =
      detail.data?.status === 'clarification'
      || SUBSTANTIVE_REASON_STATUSES.has(toStatus)
    if (requiresSubstantiveReason && reasonTooShort(note)) return
    transition.mutate({ to_status: toStatus, note })
  }

  function openCaseClosure() {
    setCloseStatus('')
    setCloseReason('')
    closeCase.reset()
    setClosingCase(true)
  }

  function submitCaseClosure(e: FormEvent) {
    e.preventDefault()
    const reason = closeReason.trim()
    if (closeCase.isPending || !closeStatus || reasonTooShort(reason)) return
    closeCase.mutate({ to_status: closeStatus, reason })
  }

  function openNonRequestClosure(reason = '') {
    setNonRequestReason(reason)
    closeNonRequest.reset()
    setClosingNonRequest(true)
  }

  function submitNonRequestClosure(e: FormEvent) {
    e.preventDefault()
    const reason = nonRequestReason.trim()
    if (closeNonRequest.isPending || reasonTooShort(reason)) return
    closeNonRequest.mutate({ reason })
  }

  function openMerge(target = '', reason = '') {
    setMergeTarget(target)
    setMergeReason(reason)
    merge.reset()
    setMerging(true)
  }

  // A confirmed intake label opens the ordinary action, prefilled. The
  // officer still reads it, edits the reason if they like, and submits.
  function applyIntakeLabel(label: IntakeLabel, target: string | null) {
    const reason = `Intake classification confirmed: ${INTAKE_LABEL_TEXT[label].toLowerCase()}${
      target ? ` ${target}` : ''
    }.`
    const action = intakeAction(label, target, Boolean(detail.data?.email_intake_auto_created))
    if (action === 'add_to_case' && target) {
      setAddToCaseDestination(target)
      setAddingToCase(true)
    } else if (action === 'merge' && target) {
      openMerge(target, reason)
    } else if (action === 'close_non_request') {
      openNonRequestClosure(reason)
    }
  }

  // Correct on an Inbox row (#33) recorded agreement and sent the officer
  // here with the label; open the same prefilled action once, then drop the
  // parameters so a reload does not reopen it.
  const [searchParams, setSearchParams] = useSearchParams()
  const inboxAction = searchParams.get('intake_action') as IntakeLabel | null
  const inboxTarget = searchParams.get('intake_target')
  const inboxActionApplied = useRef(false)
  useEffect(() => {
    if (!inboxAction || !detail.data || !showOverview || inboxActionApplied.current) return
    inboxActionApplied.current = true
    if (canManageRequest && inboxAction in INTAKE_LABEL_TEXT) {
      applyIntakeLabel(inboxAction, inboxTarget)
    }
    const next = new URLSearchParams(searchParams)
    next.delete('intake_action')
    next.delete('intake_target')
    setSearchParams(next, { replace: true })
  })

  function openFerpaVerification() {
    setFerpaMethod('in_person_photo_id')
    setFerpaNote('')
    verifyFerpaIdentity.reset()
    setVerifyingFerpa(true)
  }

  function submitFerpaVerification(e: FormEvent) {
    e.preventDefault()
    if (ferpaNote.trim().length < 20) return
    verifyFerpaIdentity.mutate()
  }

  function openAttestation() {
    setAttestVia('nextrequest')
    setAttestDate('')
    setAttestEvidence('')
    attestResponse.reset()
    setAttesting(true)
  }

  function submitAttestation(e: FormEvent) {
    e.preventDefault()
    if (attestEvidence.trim().length < 20) return
    attestResponse.mutate()
  }

  function submitMerge(e: FormEvent) {
    e.preventDefault()
    if (mergeTarget.trim().length === 0 || reasonTooShort(mergeReason.trim())) return
    merge.mutate()
  }

  if (detail.isPending) return <Spinner />
  if (detail.isError) return <ErrorNote error={detail.error} />

  const r = detail.data
  const ownerOptions = ownerChoices.data
    ? (unwrap(ownerChoices.data) ?? [])
    : []
  const responsePlan = r.initial_response_plan
  const pendingResponsePlanCopy: ResponsePlanCopy = {
    title: 'Deadline calculation pending',
    description: 'The automatic safety net will arm after the statutory due date is calculated.',
    tone: 'neutral',
  }
  const responsePlanCopy: Record<string, ResponsePlanCopy> = {
    delivered: {
      title: 'Initial response delivered',
      description: 'The statutory initial-response requirement is recorded as delivered.',
      tone: 'green',
    },
    scheduled: {
      title: 'Safety net armed',
      description: `If no initial response is delivered first, automatic queueing begins on ${responsePlan?.due_date ?? 'the due date'}.`,
      tone: 'blue',
    },
    due_for_safety_net: {
      title: 'Safety net is due now',
      description: 'No initial response is recorded as delivered. The hourly safety-net worker will attempt to queue the acknowledgment.',
      tone: 'amber',
    },
    manual_required: {
      title: 'Human acknowledgment required',
      description: 'Automatic requester mail is off. Prepare, review, approve, and queue the acknowledgment in Messages.',
      tone: 'amber',
    },
    automatic_blocked: {
      title: 'Automatic acknowledgment held',
      description: 'An automatic acknowledgment exists but manual-only mode prevents delivery. Cancel the unsent automatic message, then prepare and approve a human acknowledgment.',
      tone: 'red',
    },
    queued: {
      title: 'Acknowledgment queued',
      description: 'Delivery is in progress. The request does not become acknowledged until the mail provider confirms success.',
      tone: 'blue',
    },
    attention_required: {
      title: 'Initial response needs attention',
      description: 'An acknowledgment delivery failed or was cancelled; staff review is required.',
      tone: 'red',
    },
    terminal_excluded: {
      title: 'Closed – no response was owed',
      description: 'This request was closed in writing as not a records request, so no five-day response was ever due and the safety net will not queue a message.',
      tone: 'neutral',
    },
    merged: {
      title: 'Merged into the case of record',
      description: `This duplicate was merged into ${detail.data?.merged_into ?? 'another case'}; the five-day response is tracked there.`,
      tone: 'neutral',
    },
    terminal_unverified: {
      title: 'Closed with no five-day response on record',
      description: 'This case reached a terminal status without a confirmed RCW 42.56.520 first response. The safety net excludes terminal requests, so nothing will ever cover this automatically. Confirm whether a response was sent and record it, or document why none was owed.',
      tone: 'red',
    },
    human_draft: {
      title: 'Acknowledgment draft exists',
      description: 'A draft alone does not satisfy or disable the safety net. Approve, queue, and confirm delivery before the due date.',
      tone: 'amber',
    },
    approved_not_queued: {
      title: 'Acknowledgment approved but not queued',
      description: 'Queue it for delivery; approval alone does not satisfy or disable the safety net.',
      tone: 'amber',
    },
    not_applicable: {
      title: 'No RCW 42.56.520 response owed',
      description: `This matter is governed by other law (${r.matter_kind_display}). No five-day response is due and the safety net never covers it.`,
      tone: 'neutral',
    },
    deadline_calculating: pendingResponsePlanCopy,
  }
  const responsePlanStatus = responsePlanCopy[responsePlan?.state] ?? pendingResponsePlanCopy
  const closingClarification = r.status === 'clarification'
  const terminalRequest = isTerminalStatus(r.status)
  // An open clarification cannot be moved between working states at all: it
  // leaves only through the resolution workflow in Messages or through a
  // written closure. Offering "Change status" there offers nothing.
  const transitionChoices = MANUAL_STATUS_CHOICES
    .filter((choice) => choice.value !== r.status)
  const transitionLabel = 'Change status'
  const requiresSubstantiveReason = SUBSTANTIVE_REASON_STATUSES.has(toStatus)
  const noteHint = toStatus === 'fulfilled'
    ? reasonHint('explain why production is complete')
    : 'Optional — recorded on the timeline'
  const closureHint = closeStatus === 'closed_withdrawn'
    ? reasonHint('document the requester withdrawal')
    : closeStatus === 'closed_abandoned'
      ? reasonHint('document the basis for abandonment')
      : reasonHint('this is recorded on the timeline and in the audit trail')
  const emailIntakeReceiptId = r.email_intake_receipt_id
  const showActionZone = (showOverview || showClose)
    && !terminalRequest
    && (canManageRequest || canCloseCase)

  return (
    <>
      <div className="grid min-w-0 gap-6 lg:grid-cols-2">
        {showOverview && <Card title="Request" className="lg:col-span-2">
          <p className="mb-1 font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
            Description
          </p>
          <RequestText text={r.description} />
          <p className="mt-2 text-xs italic text-ink-muted">
            Legal record of the request as received — never edited (RCW 42.56).
          </p>
          {r.merged_sources && r.merged_sources.length > 0 && (
            <p className="mt-2 text-sm text-ink">
              Includes merged {r.merged_sources.length === 1 ? 'case' : 'cases'}{' '}
              {r.merged_sources.map((source, index) => (
                <span key={source}>
                  {index > 0 ? ', ' : ''}
                  <Link className="font-semibold underline" to={`/requests/${source}`}>
                    {source}
                  </Link>
                </span>
              ))}
              . Their requester emails are on this case; their own timelines and
              sent messages stay on record there.
            </p>
          )}
          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                Channel
              </p>
              <p className="mt-1 text-sm text-ink">{CHANNEL_LABELS[r.channel] ?? r.channel}</p>
            </div>
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                Requester
              </p>
              {r.requester ? (
                <>
                  <p className="mt-1 text-sm text-ink">{r.requester.name}</p>
                  <p className="break-all text-sm text-ink-muted">{r.requester.email}</p>
                  <p className="text-sm text-ink-muted">{r.requester.phone || '—'}</p>
                  {r.requester.company && (
                    <p className="text-sm text-ink-muted">{r.requester.company}</p>
                  )}
                  {(r.requester.mailing_address || r.requester.city || r.requester.state || r.requester.postal_code) && (
                    <address className="mt-1 text-sm not-italic text-ink-muted">
                      {r.requester.mailing_address && <div>{r.requester.mailing_address}</div>}
                      <div>
                        {[r.requester.city, r.requester.state].filter(Boolean).join(', ')}{' '}
                        {r.requester.postal_code}
                      </div>
                    </address>
                  )}
                </>
              ) : <p className="mt-1 text-sm text-ink-muted">Restricted</p>}
            </div>
            <div>
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                Assigned officer
              </p>
              {canManageRequest ? (
                <Select
                  aria-label="Case owner"
                className="mt-1 w-full min-w-0 max-w-full"
                  value={r.assigned_officer_id == null ? '' : String(r.assigned_officer_id)}
                  disabled={ownerChoices.isPending || assignOwner.isPending}
                  onChange={(event) => {
                    const value = event.target.value
                    assignOwner.mutate(value === '' ? null : Number(value))
                  }}
                >
                  <option value="">Unassigned</option>
                  {ownerOptions.map((user) => (
                    <option key={user.id} value={user.id}>{user.display_name}</option>
                  ))}
                </Select>
              ) : (
                <p className="mt-1 text-sm text-ink">
                  {r.assigned_officer_username ?? '—'}
                </p>
              )}
              {ownerChoices.isError && <ErrorNote error={ownerChoices.error} />}
            </div>
          </div>
        </Card>}

        {/* The action zone.
            This block is deliberately indivisible: the clarification notice
            explains the very button beside it ("use Close request; no response
            will be fabricated"), so nothing may be rendered between them. It
            sits directly under the request text because that text is what
            justifies the decision, and directly above Deadlines because the
            clock is what makes it urgent. */}
        {showActionZone && showOverview && canManageRequest && !terminalRequest && (
          <div className="lg:col-span-2">
            <IntakeClassificationCard
              number={r.number}
              emailAutoCreated={Boolean(r.email_intake_auto_created)}
              onConfirm={applyIntakeLabel}
            />
          </div>
        )}
        {showActionZone && (
          <div className="lg:col-span-2">
            {closingClarification && (
              <div className="mb-3 rounded-md border border-warning bg-warning-soft px-4 py-3 text-sm text-warning-ink">
                Resolve the clarification from Messages after reviewing
                the received response and recording substantive resolution notes. If
                the requester withdrew or the documented clarification period expired,
                use Close case; no response will be fabricated.
              </div>
            )}
            <div className="flex flex-wrap justify-end gap-3">
              {canManageRequest && r.email_intake_auto_created && (
                <Button
                  variant="ghost"
                  disabled={!emailIntakeReceiptId}
                  onClick={() => {
                    setAddToCaseDestination('')
                    setAddingToCase(true)
                  }}
                >
                  Add to an existing case…
                </Button>
              )}
              {canManageRequest && r.email_intake_auto_created && (
                <Button variant="ghost" onClick={() => openNonRequestClosure()}>
                  Close as non-request
                </Button>
              )}
              {/* A merge closes this number and moves the requester's
                  correspondence, so it stays with the Records Officer. */}
              {canManageRequest && (
                <Button variant="ghost" onClick={() => openMerge()}>
                  Merge into case…
                </Button>
              )}
              {canCloseCase && (
                <Button variant="ghost" onClick={openCaseClosure}>
                  Close case…
                </Button>
              )}
              {canManageRequest && !closingClarification && (
                <Button onClick={openTransition}>{transitionLabel}</Button>
              )}
            </div>
          </div>
        )}
        {/* A closed case still has one action: reopening it. That is ordinary
            records practice -- a requester follows up, a closure turns out to
            have been premature, records surface late -- but it reverses a
            decision made in writing, so it asks for the same accountability
            the closure did. The closure itself stays on the timeline. */}
        {/* A merged duplicate has no next action here at all: its emails and
            its five-day obligation live on the case of record, and reopening
            it would leave an empty case with a retired number. */}
        {(showOverview || showClose) && terminalRequest && r.merged_into && (
          <div className="lg:col-span-2 rounded-md border border-line bg-surface-sunken px-4 py-3 text-sm text-ink">
            This case was merged into{' '}
            <Link className="font-semibold underline" to={`/requests/${r.merged_into}`}>
              {r.merged_into}
            </Link>
            . Every requester email moved there, and that case keeps its own
            received date and five-day clock. This number stays closed as a
            duplicate and cannot be reopened.
          </div>
        )}
        {(showOverview || showClose) && canManageRequest && terminalRequest && !r.merged_into && (
          <div className="lg:col-span-2 flex flex-wrap items-center justify-end gap-3">
            <p className="mr-auto text-sm text-ink-muted">
              This request is closed. Reopening it is recorded on the timeline
              with your name and reason; it does not start a new five-day
              response clock.
            </p>
            <Button
              onClick={() => {
                reopen.reset()
                setReopenReason('')
                setReopenStatus('gathering')
                setReopening(true)
              }}
            >
              Reopen request
            </Button>
          </div>
        )}
        {/* A case the system closed on its own (#34) says so, with Undo. */}
        {showOverview && canManageRequest && r.status === 'closed_non_request' && (
          <div className="lg:col-span-2">
            <IntakeClassificationCard
              number={r.number}
              emailAutoCreated={Boolean(r.email_intake_auto_created)}
              onConfirm={applyIntakeLabel}
              closed
            />
          </div>
        )}

        {showScope && canManageRequest && (
          <Card title="Request-intent advisory" className="lg:col-span-2">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="max-w-3xl">
                <p className="text-sm text-ink-muted">
                  Advisory only. Model output is untrusted and permanently not
                  applied. It cannot change routing, scope, fees, estimates,
                  status, or requester correspondence.
                </p>
                {intentAdvisory.data?.latest && (
                  <p className="mt-1 text-xs text-ink-muted">
                    Model {intentAdvisory.data.latest.model_identifier} · input SHA-256{' '}
                    {intentAdvisory.data.latest.input_sha256.slice(0, 16)}…
                    {intentAdvisory.data.latest.input_truncated ? ' · input truncated' : ''}
                  </p>
                )}
              </div>
              <Button
                variant="ghost"
                disabled={
                  !intentAdvisory.data?.enabled
                  || requestIntentAdvisory.isPending
                  || ['pending', 'running'].includes(intentAdvisory.data?.latest?.state ?? '')
                }
                onClick={() => requestIntentAdvisory.mutate()}
              >
                {requestIntentAdvisory.isPending ? 'Queueing…' : 'Run advisory'}
              </Button>
            </div>
            {intentAdvisory.isError && <ErrorNote error={intentAdvisory.error} />}
            {intentAdvisory.data && !intentAdvisory.data.enabled && (
              <p className="mt-3 text-sm text-ink-muted">
                Turned off in this environment. It is a configuration setting
                (PRR_AI_ADVISORY_ENABLED plus a Bedrock region and model), not
                a pending approval — an administrator can switch it on.
              </p>
            )}
            {intentAdvisory.data?.latest?.state === 'failed' && (
              <p className="mt-3 text-sm text-danger-ink">
                {intentAdvisory.data.latest.failure_detail}
              </p>
            )}
            {['pending', 'running'].includes(intentAdvisory.data?.latest?.state ?? '') && (
              <p className="mt-3 text-sm text-ink-muted">Analysis in progress…</p>
            )}
            {intentAdvisory.data?.latest?.state === 'complete' && (
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                <div>
                  <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Proposed summary
                  </p>
                  <p className="mt-1 text-sm text-ink">
                    {intentAdvisory.data.latest.proposal.summary}
                  </p>
                </div>
                <div>
                  <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Required search anchors
                  </p>
                  <p className="mt-1 text-sm text-ink">
                    {intentAdvisory.data.latest.proposal.required_search_terms?.join(' · ') || 'None'}
                  </p>
                  <p className="mt-3 font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Alternative search terms
                  </p>
                  <p className="mt-1 text-sm text-ink">
                    {intentAdvisory.data.latest.proposal.search_terms?.join(' · ') || 'None'}
                  </p>
                </div>
                <div>
                  <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Proposed systems
                  </p>
                  <p className="mt-1 text-sm text-ink">
                    {intentAdvisory.data.latest.proposal.record_system_slugs?.join(' · ') || 'None'}
                  </p>
                </div>
                <div>
                  <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Proposed date range
                  </p>
                  <p className="mt-1 text-sm text-ink">
                    {intentAdvisory.data.latest.proposal.date_range?.start || 'No start proposed'}
                    {' to '}
                    {intentAdvisory.data.latest.proposal.date_range?.end || 'the receipt date'}
                  </p>
                  <p className="mt-1 text-xs text-ink-muted">
                    Only records in existence when the request arrived can be
                    responsive, so the end never runs past the receipt date.
                  </p>
                </div>
                <div>
                  <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Narrowing options to offer
                  </p>
                  {intentAdvisory.data.latest.proposal.narrowing_options?.length ? (
                    <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-ink">
                      {intentAdvisory.data.latest.proposal.narrowing_options.map((option) => (
                        <li key={option.rationale}>
                          {option.rationale}
                          <span className="block text-xs text-ink-muted">
                            {option.terms.join(' · ')}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : <p className="mt-1 text-sm text-ink">None</p>}
                  <p className="mt-1 text-xs text-ink-muted">
                    Drafting material for a human scope conversation. Nothing
                    here narrows the request or reaches the requester.
                  </p>
                </div>
                <div>
                  <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Questions for human consideration
                  </p>
                  {intentAdvisory.data.latest.proposal.clarification_questions?.length ? (
                    <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-ink">
                      {intentAdvisory.data.latest.proposal.clarification_questions.map((question) => (
                        <li key={question}>{question}</li>
                      ))}
                    </ul>
                  ) : <p className="mt-1 text-sm text-ink">None</p>}
                </div>
              </div>
            )}
          </Card>
        )}

        {showScope && canManageRequest && (
          <Card title="Google Vault scope count" className="lg:col-span-2">
            <p className="text-sm text-ink-muted">
              Count possible Gmail messages, Drive files, and Chat messages for
              human-reviewed terms, district custodians, and an optional date
              range. This does not create a hold, collection task, status change,
              or requester message. The first count may create the request&apos;s
              empty Vault matter.
            </p>
            <p className="mt-1 text-xs text-ink-muted">
              Google cannot count Drive or Chat directly, so Vault counts them
              with a short-lived export that is deleted as soon as it is counted
              and is never downloaded; those counts can take longer. A count is a scoping estimate, not a
              responsiveness determination.
            </p>
            {vaultCounts.isError && <ErrorNote error={vaultCounts.error} />}
            {vaultCounts.data && !vaultCounts.data.enabled && (
              <p className="mt-3 text-sm text-warning-ink">
                Vault counting is unavailable until the delegated Vault identity is configured.
              </p>
            )}
            <form
              className="mt-4 space-y-4"
              onSubmit={(event) => {
                event.preventDefault()
                requestVaultCount.mutate()
              }}
            >
              <Field
                label="Vault search query"
                hint="Review and edit the advisor terms. Google Vault search operators are supported."
              >
                <Textarea
                  aria-label="Vault search query"
                  rows={3}
                  value={vaultTerms}
                  onChange={(event) => setVaultTerms(event.target.value)}
                  required
                  maxLength={1000}
                />
              </Field>
              <fieldset>
                <legend className="font-heading text-sm font-bold text-ink">
                  Account scope
                </legend>
                <p className="mt-1 text-xs text-ink-muted">
                  Choose the narrowest scope you can support. All district accounts may take longer.
                </p>
                <div className="mt-2 flex flex-wrap gap-5 text-sm text-ink">
                  <label className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="vault-search-scope"
                      value="accounts"
                      checked={vaultSearchScope === 'accounts'}
                      onChange={() => setVaultSearchScope('accounts')}
                    />
                    Specific district accounts
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="vault-search-scope"
                      value="entire_org"
                      checked={vaultSearchScope === 'entire_org'}
                      onChange={() => setVaultSearchScope('entire_org')}
                    />
                    All district accounts
                  </label>
                </div>
              </fieldset>
              <Field
                label="Custodian accounts"
                hint={vaultSearchScope === 'entire_org'
                  ? 'Not used when searching all district accounts.'
                  : 'District email accounts only; separate addresses with spaces, commas, or new lines.'}
              >
                <Textarea
                  aria-label="Custodian accounts"
                  rows={3}
                  value={vaultCustodians}
                  onChange={(event) => {
                    vaultTouched.current.custodians = true
                    setVaultCustodians(event.target.value)
                  }}
                  required={vaultSearchScope === 'accounts'}
                  disabled={vaultSearchScope === 'entire_org'}
                  placeholder="name@psd401.net"
                />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Start date" hint="Optional; interpreted in Pacific time.">
                  <Input
                    aria-label="Start date"
                    type="date"
                    value={vaultStartDate}
                    onChange={(event) => {
                      vaultTouched.current.start = true
                      setVaultStartDate(event.target.value)
                    }}
                  />
                </Field>
                <Field
                  label="End date"
                  hint={
                    'Inclusive and interpreted in Pacific time. Defaults to the '
                    + 'receipt date, because only records in existence when the '
                    + 'request arrived can be responsive. Clear or change it if '
                    + 'this case calls for a different end.'
                  }
                >
                  <Input
                    aria-label="End date"
                    type="date"
                    value={vaultEndDate}
                    min={vaultStartDate || undefined}
                    onChange={(event) => {
                      vaultTouched.current.end = true
                      setVaultEndDate(event.target.value)
                    }}
                  />
                </Field>
              </div>
              <Button
                type="submit"
                disabled={
                  !vaultCounts.data?.enabled
                  || requestVaultCount.isPending
                  || !vaultTerms.trim()
                  || (vaultSearchScope === 'accounts' && !vaultCustodians.trim())
                  || ['pending', 'running'].includes(vaultCounts.data?.latest?.state ?? '')
                }
              >
                {requestVaultCount.isPending ? 'Queueing…' : 'Count possible items'}
              </Button>
            </form>
            {requestVaultCount.isError && <ErrorNote error={requestVaultCount.error} />}
            {['pending', 'running'].includes(vaultCounts.data?.latest?.state ?? '') && (
              <div className="mt-4">
                <p className="text-sm text-ink-muted">Vault count in progress…</p>
                <VaultSourceCounts rows={vaultCounts.data?.latest?.source_counts} />
              </div>
            )}
            {vaultCounts.data?.latest?.state === 'failed' && (
              <p className="mt-4 text-sm text-danger-ink">
                {vaultCounts.data.latest.failure_detail}
              </p>
            )}
            {vaultCounts.data?.latest?.state === 'complete' && (
              <div className="mt-5 rounded-lg border border-flow bg-surface-sunken p-4">
                <p className="font-heading text-2xl font-bold text-ink">
                  {new Intl.NumberFormat().format(vaultCounts.data.latest.total_count ?? 0)}{' '}
                  {countHeadline(vaultCounts.data.latest.sources)}
                </p>
                <VaultSourceCounts rows={vaultCounts.data.latest.source_counts} />
                <p className="mt-1 text-sm text-ink-muted">
                  This is a search-universe estimate, not a responsiveness determination.
                </p>
                <p className="mt-1 text-xs text-ink-muted">
                  Scope: {vaultCounts.data.latest.search_scope === 'entire_org'
                    ? 'all district accounts'
                    : `${vaultCounts.data.latest.custodian_emails.length} specified district account(s)`}
                </p>
                <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-4">
                  <div>
                    <dt className="text-ink-muted">Accounts queried</dt>
                    <dd className="font-heading font-bold text-ink">
                      {vaultCounts.data.latest.queried_accounts_count ?? 0}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-muted">Accounts matching</dt>
                    <dd className="font-heading font-bold text-ink">
                      {vaultCounts.data.latest.matching_accounts_count ?? 0}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-muted">Not queryable</dt>
                    <dd className="font-heading font-bold text-ink">
                      {vaultCounts.data.latest.non_queryable_accounts_count ?? 0}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-muted">Account errors</dt>
                    <dd className="font-heading font-bold text-ink">
                      {vaultCounts.data.latest.accounts_with_errors_count ?? 0}
                    </dd>
                  </div>
                </dl>
                <p className="mt-3 text-xs text-ink-muted">
                  Input SHA-256 {vaultCounts.data.latest.input_sha256.slice(0, 16)}… ·{' '}
                  {vaultCounts.data.latest.trigger === 'automatic'
                    ? 'run automatically when the request arrived — review the scope before collecting'
                    : `requested by ${vaultCounts.data.latest.requested_by_username ?? 'unknown'}`}
                </p>
                <Button
                  className="mt-4"
                  variant="secondary"
                  type="button"
                  onClick={() =>
                    navigate(
                      `/requests/${r.number}/messages?compose=scope_clarification`
                      + `&count=${vaultCounts.data?.latest?.total_count ?? 0}`,
                    )
                  }
                >
                  Write the scope conversation
                </Button>
                {(vaultCounts.data.latest.collectable
                  ?? (vaultCounts.data.latest.total_count ?? 0) > 0)
                  && (vaultCollectionState == null || vaultCollectionRetryable) && (
                  <Button
                    className="mt-4"
                    type="button"
                    onClick={() => {
                      setVaultCollectionConfirmed(false)
                      setVaultCollectionOpen(true)
                    }}
                  >
                    {vaultCollectionRetryable
                      ? 'Retry Vault collection'
                      : 'Start Vault collection'}
                  </Button>
                )}
                {vaultCollectionState === 'failed' && (
                  <div className="mt-4 rounded-md border border-danger bg-danger-soft p-3 text-sm text-danger-ink">
                    <p className="font-semibold">Vault collection needs attention.</p>
                    <p className="mt-1">
                      {vaultCollectionRetryable
                        ? 'The worker handoff failed before a Vault export was recorded. Review the scope and retry the same collection task.'
                        : 'Review the failed export or ingest evidence under Collection tasks before choosing the next action.'}
                    </p>
                    <Link
                      to={`/requests/${r.number}/collect`}
                      className="mt-2 inline-block underline"
                    >
                      Open Collection tasks
                    </Link>
                  </div>
                )}
                {vaultCollectionState != null && vaultCollectionState !== 'failed' && (
                  <div className="mt-4 rounded-md border border-flow bg-surface-raised p-3 text-sm text-ink">
                    <p className="font-semibold">Vault export queued.</p>
                    <p className="mt-1 text-ink-muted">
                      Follow it in{' '}
                      <Link to={`/requests/${r.number}/collect`} className="text-accent underline">
                        Collection tasks
                      </Link>
                      . After ingestion, classify the exported messages in{' '}
                      <Link to={`/requests/${r.number}/review?view=records`} className="text-accent underline">
                        Record items
                      </Link>
                      .
                    </p>
                  </div>
                )}
              </div>
            )}
          </Card>
        )}

        {showOverview && <Card title="Deadlines">
          <dl className="space-y-3 text-sm">
            <div className="flex justify-between gap-3">
              <dt className="text-ink-muted">Matter kind</dt>
              <dd className="font-heading font-bold text-ink">
                {r.matter_kind_display}
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-ink-muted">Received</dt>
              <dd className="font-heading font-bold text-ink">{formatDay(r.received_at)}</dd>
            </div>
            {r.matter_kind === 'public_records' && (
              <div className="flex justify-between gap-3">
                <dt className="text-ink-muted">Statutory response due</dt>
                <dd className="font-heading font-bold text-ink">
                  {formatDay(r.statutory_response_due)}
                </dd>
              </div>
            )}
            {r.matter_kind === 'ferpa_parent' && (
              <div className="flex justify-between gap-3">
                <dt className="text-ink-muted">FERPA access due (45 days)</dt>
                <dd className="font-heading font-bold text-ink">
                  {formatDay(r.ferpa_access_due)}
                </dd>
              </div>
            )}
            <div className="flex justify-between gap-3">
              <dt className="text-ink-muted">Estimated completion</dt>
              <dd className="font-heading font-bold text-ink">
                {formatDay(r.estimated_completion)}
              </dd>
            </div>
            {r.matter_kind === 'public_records' && (
              <div className="flex justify-between gap-3">
                <dt className="text-ink-muted">Tolled business days</dt>
                <dd className="font-heading font-bold text-ink">
                  {r.tolled_business_days}
                </dd>
              </div>
            )}
            <div className="flex justify-between gap-3">
              <dt className="text-ink-muted">Acknowledgment sent</dt>
              <dd className="font-heading font-bold text-ink">
                {formatDay(r.acknowledgment_sent_at)}
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-ink-muted">Closed</dt>
              <dd className="font-heading font-bold text-ink">
                {formatDay(r.closed_at)}
              </dd>
            </div>
          </dl>
          <div className="mt-5 border-t border-line pt-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={responsePlanStatus.tone}>{responsePlanStatus.title}</Badge>
              {responsePlan?.automated === true && (
                <span className="text-xs text-ink-muted">Automated safety-net message</span>
              )}
            </div>
            <p className="mt-2 text-sm text-ink-muted">{responsePlanStatus.description}</p>
            {r.external_initial_response && (
              <p className="mt-2 text-sm text-ink">
                Recorded as sent through {r.external_initial_response.sent_via_label}
                {r.external_initial_response.responded_on
                  ? ` on ${r.external_initial_response.responded_on}`
                  : ', date not on record'}
                {r.external_initial_response.attested_by
                  ? ` by ${r.external_initial_response.attested_by}`
                  : ''}
                . {r.external_initial_response.evidence}
              </p>
            )}
            {r.matter_kind === 'ferpa_parent' && (
              <div className="mt-3 border-t border-line pt-3">
                {r.ferpa_identity_verification ? (
                  <p className="text-sm text-ink">
                    Identity verified under 34 CFR 99.10 by{' '}
                    {r.ferpa_identity_verification.method_display.toLowerCase()}
                    {r.ferpa_identity_verification.verified_by
                      ? ` — ${r.ferpa_identity_verification.verified_by}`
                      : ''}
                    , {formatDay(r.ferpa_identity_verification.verified_at)}.{' '}
                    {r.ferpa_identity_verification.note}
                  </p>
                ) : (
                  <>
                    <p className="text-sm text-ink-muted">
                      No identity verification on record. Nothing may be
                      disclosed to this requester until one is.
                    </p>
                    {canManageRequest && !terminalRequest && (
                      <div className="mt-2">
                        <Button variant="ghost" onClick={openFerpaVerification}>
                          Record identity verification…
                        </Button>
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
            {canManageRequest && !terminalRequest && !r.initial_response_delivered
              && r.matter_kind === 'public_records' && (
              <div className="mt-3">
                <Button variant="ghost" onClick={openAttestation}>
                  Record a response sent outside this system…
                </Button>
              </div>
            )}
            {responsePlan?.next_action && (
              <p className="mt-2 font-heading text-sm font-bold text-ink">
                {`Next: ${responsePlan.next_action}`}
              </p>
            )}
            {(responsePlan?.steps?.length ?? 0) > 0 && (
              <ol
                aria-label="Statutory response steps"
                className="mt-3 space-y-1.5 text-sm"
              >
                {responsePlan.steps.map((step) => (
                  <li key={step.key} className="flex items-start gap-2">
                    <span
                      aria-hidden="true"
                      className={`mt-1.5 size-2 shrink-0 rounded-full ${
                        step.state === 'done'
                          ? 'bg-flow'
                          : step.state === 'current'
                            ? 'bg-accent'
                            : 'bg-line-strong'
                      }`}
                    />
                    <span
                      // ink-muted is the lightest ink token that clears WCAG 2
                      // AA at 14px in every theme.
                      className={
                        step.state === 'current'
                          ? 'font-heading font-semibold text-ink'
                          : step.state === 'done'
                            ? 'text-ink-muted line-through'
                            : 'text-ink-muted'
                      }
                    >
                      {step.label}
                      <span className="sr-only">
                        {step.state === 'done'
                          ? ' (complete)'
                          : step.state === 'current'
                            ? ' (do this next)'
                            : ' (not started)'}
                      </span>
                    </span>
                  </li>
                ))}
              </ol>
            )}
            {responsePlan?.state !== 'delivered' && (
              <p className="mt-2 text-xs text-ink-muted">
                {responsePlan?.automatic_check ?? 'Hourly at :30 Pacific on and after the due date.'}
              </p>
            )}
            {canManageRequest && (
              <div className="mt-3 flex flex-wrap gap-4 text-sm">
                <Link to="/configuration/message-templates" className="text-accent underline-offset-2 hover:underline">
                  Review five-day response template
                </Link>
                <Link
                  to={`/requests/${r.number}/messages`}
                  className="text-accent underline-offset-2 hover:underline"
                >
                  Prepare a human acknowledgment
                </Link>
              </div>
            )}
          </div>
        </Card>}

        {showOverview && <Card title="Estimate">
          <p className="text-sm text-ink-muted">Current estimated completion</p>
          <p className="mt-1 font-heading text-2xl font-bold text-ink">
            {formatDay(r.estimated_completion)}
          </p>
          {canManageRequest && (
            <p className="mt-2 text-xs text-ink-muted">
              Draft estimate revisions in Messages; the
              estimate changes only after confirmed delivery.
            </p>
          )}
          {r.estimate_revisions.length > 0 && (
            <div className="mt-4 space-y-2 border-t border-line pt-3">
              <p className="font-heading text-xs font-bold uppercase tracking-wide text-ink-muted">
                Revision history
              </p>
              {r.estimate_revisions.map((rev, i) => (
                <div key={i} className="text-sm text-ink">
                  <span className="font-heading font-bold">
                    {rev.previous_date ?? '—'} → {rev.new_date}
                  </span>
                  <span className="text-ink-muted">
                    {canManageRequest ? (
                      <>
                        {' '}— {rev.reason} ({rev.automated ? 'automated' : rev.revised_by ?? 'system'},{' '}
                        {formatDay(rev.created_at)})
                      </>
                    ) : (
                      <> ({formatDay(rev.created_at)})</>
                    )}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>}

        {/* The running log.
            Overview used to render its own five StatusEvents from the request
            payload while the Activity tab rendered a different, server-merged
            feed. One case with two histories that can disagree is worse than
            either, so both now read the same endpoint through the same
            component. Migration compliance notes keep their slots here
            regardless of age; see boundedActivity. */}
        {showOverview && (
          <CaseActivityFeed number={r.number} title="Running log" />
        )}

        {/* Beside the running log, which records what the *system* did. This
            records what the people did: who was called, what they said, what
            the case is waiting on. Internal, and never sent. */}
        {showOverview && (
          <CaseNotesPanel number={r.number} className="lg:col-span-2" />
        )}

        {/* Secondary reference below the log: what arrived with the request
            and where collection stands. Both were full-width cards competing
            with the request text for the top of the page. */}
        {showOverview && canManageRequest && (
          <Card title="Intake attachments" className="lg:col-span-2">
            {r.intake_attachments.length === 0 ? (
              <EmptyState>No files were attached at intake.</EmptyState>
            ) : (
              <div className="space-y-3">
                {r.intake_attachments.map((attachment) => {
                  const scan = ATTACHMENT_STATUS[attachment.scan_status]
                  return (
                    <div
                      key={attachment.id}
                      className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line p-3"
                    >
                      <div>
                        <p className="font-heading text-sm font-bold text-ink">
                          {attachment.original_filename}
                        </p>
                        <p className="text-xs text-ink-muted">
                          {humanFileSize(attachment.size)} · SHA-256 {attachment.sha256.slice(0, 12)}…
                        </p>
                      </div>
                      <div className="flex items-center gap-3">
                        <Badge tone={scan.tone}>{scan.label}</Badge>
                        {attachment.scan_status === 'clean' && (
                          <Button
                            variant="ghost"
                            disabled={downloadAttachment.isPending}
                            onClick={() => downloadAttachment.mutate(attachment.id)}
                          >
                            Download
                          </Button>
                        )}
                      </div>
                    </div>
                  )
                })}
                {downloadAttachment.isError && <ErrorNote error={downloadAttachment.error} />}
              </div>
            )}
          </Card>
        )}

        {showOverview && <Card title="Collection status" className="lg:col-span-2">
          {r.collection_tasks.length === 0 ? (
            <EmptyState>No collection tasks recorded.</EmptyState>
          ) : (
            <Table
              columns={[
                { header: 'System', cell: (t: CollectionTask) => t.system ?? '—' },
                { header: 'Kind', cell: (t: CollectionTask) => TASK_KIND_LABELS[t.kind] ?? t.kind },
                {
                  header: 'State',
                  cell: (t: CollectionTask) => (
                    <Badge tone={TASK_STATE_TONES[t.state] ?? 'neutral'}>
                      {TASK_STATE_LABELS[t.state] ?? t.state}
                    </Badge>
                  ),
                },
                { header: 'Due', cell: (t: CollectionTask) => t.due_date ?? '—' },
              ]}
              rows={r.collection_tasks.slice(0, 5)}
              rowKey={(t) => t.id}
            />
          )}
          <Link
            to={`/requests/${r.number}/collect`}
            className="mt-4 inline-block text-sm font-semibold text-accent underline-offset-2 hover:underline"
          >
            Open collection work
          </Link>
        </Card>}

        {showCollection && r.footage_preservations.length > 0 && (
          <Card title="Footage preservation" className="lg:col-span-2">
            <p className="mb-3 text-sm text-ink-muted">
              Cameras overwrite footage on a fixed schedule. Once a request has
              been received, letting that footage age out is destruction of a
              public record under RCW 42.56.100 — holds here are racing that
              clock.
            </p>
            <div className="space-y-3">
              {r.footage_preservations.map((p) => (
                <div key={p.id} className="flex flex-wrap items-center justify-between gap-3">
                  {ACTIVE_PRESERVATION_STATES.has(p.state) ? (
                    <Badge tone="red">
                      Footage may be overwritten —{' '}
                      {PRESERVATION_STATE_LABELS[p.state] ?? p.state}
                    </Badge>
                  ) : (
                    <Badge tone={p.state === 'held' ? 'green' : 'neutral'}>
                      {PRESERVATION_STATE_LABELS[p.state] ?? p.state}
                    </Badge>
                  )}
                  {(
                    roles.includes('records_officer')
                    || roles.includes('admin')
                  ) ? (
                    <p className="text-sm text-ink-muted">
                      Hold details and authorized controls are listed below in this case.
                    </p>
                  ) : (
                    <p className="text-sm text-ink-muted">
                      <strong>Records Officer handoff required.</strong>{' '}
                      Preservation details and camera exclusions are restricted.
                    </p>
                  )}
                </div>
              ))}
            </div>
          </Card>
        )}
      </div>

      <Modal
        open={canManageRequest && terminalRequest && reopening}
        title="Reopen request"
        onClose={() => setReopening(false)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            reopen.mutate()
          }}
        >
          <p className="text-sm text-ink-muted">
            The closure stays on the timeline. Reopening does not restart the
            RCW 42.56.520 response clock, so a request that closed overdue
            reopens overdue.
          </p>
          <Field label="Reopen into">
            <Select
              value={reopenStatus}
              onChange={(event) => setReopenStatus(event.target.value)}
              required
            >
              {/* Only working statuses: acknowledged, clarification and denied
                  are true only once a message reached the requester. */}
              <option value="received">Received</option>
              <option value="gathering">Gathering Records</option>
              <option value="review">Review &amp; Redaction</option>
              <option value="installment">Producing Installments</option>
            </Select>
          </Field>
          <Field
            label="Reason"
            hint={reasonHint('record why this case is being reopened')}
          >
            <Textarea
              value={reopenReason}
              onChange={(event) => setReopenReason(event.target.value)}
              rows={4}
              required
            />
          </Field>
          {reopen.isError && <ErrorNote error={reopen.error} />}
          <div className="flex justify-end gap-3">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setReopening(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={reopen.isPending || reasonTooShort(reopenReason)}
            >
              Reopen request
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={
          canManageRequest
          && !terminalRequest
          && !closingClarification
          && transitioning
        }
        title={transitionLabel}
        onClose={closeTransition}
      >
        <form onSubmit={submitTransition} className="space-y-4">
          <Field label="New status">
            <Select value={toStatus} onChange={(e) => setToStatus(e.target.value)} required>
              <option value="" disabled>
                Select a status…
              </option>
              {transitionChoices.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label={requiresSubstantiveReason ? 'Reason' : 'Note'}
            hint={noteHint}
          >
            <Textarea
              rows={3}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              required={requiresSubstantiveReason}
              minLength={requiresSubstantiveReason ? 10 : undefined}
            />
          </Field>
          {transition.isError && <ErrorNote error={transition.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeTransition}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                transition.isPending
                || !toStatus
                || (requiresSubstantiveReason && reasonTooShort(note))
              }
            >
              {transition.isPending ? 'Saving…' : transitionLabel}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={canManageRequest && vaultCollectionOpen}
        title="Confirm Vault collection"
        onClose={() => {
          setVaultCollectionOpen(false)
          setVaultCollectionConfirmed(false)
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-ink-muted">
            This creates a collection task and starts a Vault export for the exact
            completed count below. It does not decide which records are responsive
            and it does not send a requester message.
          </p>
          <div className="rounded-lg border border-line bg-surface-sunken p-3 text-sm text-ink">
            <p className="font-semibold">
              {new Intl.NumberFormat().format(latestVaultCount?.total_count ?? 0)}{' '}
              {countHeadline(latestVaultCount?.sources)} to export
            </p>
            {vaultCollectionOpen && <VaultSourceCounts rows={latestVaultCount?.source_counts} />}
            <p className="mt-2 break-words"><span className="font-semibold">Query:</span> {latestVaultCount?.terms}</p>
            <p className="mt-1">
              <span className="font-semibold">Scope:</span>{' '}
              {latestVaultCount?.search_scope === 'entire_org'
                ? 'all district accounts'
                : latestVaultCount?.custodian_emails.join(', ')}
            </p>
            <p className="mt-1">
              <span className="font-semibold">Dates:</span>{' '}
              {latestVaultCount?.date_range_start || 'no start date'} through{' '}
              {latestVaultCount?.date_range_end || 'no end date'}
            </p>
          </div>
          {latestVaultCount?.search_scope === 'entire_org' ? (
            <p className="text-sm text-warning-ink">
              This district-wide account scope may produce a large export. Google
              Vault does not support an organization-wide hold through this path;
              the export itself remains auditable and unchanged.
            </p>
          ) : (
            <p className="text-sm text-ink-muted">
              A targeted hold on the specified district accounts (Gmail, Drive,
              and Chat) will be verified or created before each export starts.
            </p>
          )}
          <label className="flex items-start gap-2 text-sm text-ink">
            <input
              className="mt-1"
              type="checkbox"
              checked={vaultCollectionConfirmed}
              onChange={(event) => setVaultCollectionConfirmed(event.target.checked)}
            />
            <span>I reviewed this exact query, account scope, and date range.</span>
          </label>
          {startVaultCollection.isError && <ErrorNote error={startVaultCollection.error} />}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setVaultCollectionOpen(false)
                setVaultCollectionConfirmed(false)
              }}
            >
              Cancel
            </Button>
            <Button
              type="button"
              disabled={!vaultCollectionConfirmed || startVaultCollection.isPending}
              onClick={() => startVaultCollection.mutate()}
            >
              {startVaultCollection.isPending ? 'Starting…' : 'Start export'}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Closing a case ends it, so it asks for the ending and the reason
          together rather than reusing the working-state control. Every staff
          member working the case may do this; the written reason and the
          audit entry are what make it accountable. */}
      <Modal
        open={canCloseCase && !terminalRequest && closingCase}
        title="Close case"
        onClose={() => setClosingCase(false)}
      >
        <form onSubmit={submitCaseClosure} className="space-y-4">
          <p className="text-sm text-ink-muted">
            The closure, your name, and this reason go on the timeline and into
            the audit trail. Nothing is deleted and no requester message is
            sent.
            {closingClarification
              ? ' The open clarification is cancelled at the same moment; no'
                + ' response is fabricated for it.'
              : ''}
          </p>
          <Field label="How this case ended">
            <Select
              value={closeStatus}
              onChange={(e) => setCloseStatus(e.target.value)}
              required
            >
              <option value="" disabled>
                Select a closure…
              </option>
              {CASE_CLOSURE_CHOICES.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Closure reason" hint={closureHint}>
            <Textarea
              rows={3}
              value={closeReason}
              onChange={(e) => setCloseReason(e.target.value)}
              required
              minLength={10}
            />
          </Field>
          {closeCase.isError && <ErrorNote error={closeCase.error} />}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setClosingCase(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                closeCase.isPending
                || !closeStatus
                || reasonTooShort(closeReason)
              }
            >
              {closeCase.isPending ? 'Closing…' : 'Close case'}
            </Button>
          </div>
        </form>
      </Modal>

      {canManageRequest && !terminalRequest && addingToCase && (
        <AddToExistingCaseModal
          open
          sourceNumber={r.number}
          receiptId={emailIntakeReceiptId}
          initialDestination={addToCaseDestination}
          onClose={() => setAddingToCase(false)}
          onRedirected={async () => {
            setAddingToCase(false)
            await Promise.all([
              qc.invalidateQueries({ queryKey: ['request', number] }),
              qc.invalidateQueries({
                queryKey: ['case-workspace-summary', number],
              }),
              qc.invalidateQueries({ queryKey: ['case-activity', number] }),
            ])
            toast('success', 'Message added to the existing case.')
          }}
        />
      )}

      <Modal
        open={canManageRequest && !terminalRequest && closingNonRequest}
        title="Close as non-request"
        onClose={() => setClosingNonRequest(false)}
      >
        <form onSubmit={submitNonRequestClosure} className="space-y-4">
          <p className="text-sm text-ink-muted">
            This preserves the original email, request row, timeline, and audit
            trail while recording that the message did not ask for public records.
          </p>
          <Field
            label="Closure reason"
            hint={reasonHint("explain why the email is not a public records request")}
          >
            <Textarea
              rows={3}
              value={nonRequestReason}
              onChange={(e) => setNonRequestReason(e.target.value)}
              required
              minLength={10}
            />
          </Field>
          {closeNonRequest.isError && <ErrorNote error={closeNonRequest.error} />}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setClosingNonRequest(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                closeNonRequest.isPending || reasonTooShort(nonRequestReason)
              }
            >
              {closeNonRequest.isPending ? 'Closing…' : 'Close as non-request'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={canManageRequest && !terminalRequest && verifyingFerpa}
        title="Record FERPA identity verification"
        onClose={() => setVerifyingFerpa(false)}
      >
        <form onSubmit={submitFerpaVerification} className="space-y-4">
          <p className="text-sm text-ink-muted">
            34 CFR 99.10 gives the right of access to a parent or eligible
            student and to nobody else. This records your judgment that the
            person asking is that person: the method and what you relied on go
            on the timeline and the audit trail under your name. It discloses
            nothing, sends nothing, and starts no clock.
          </p>
          <Field label="How the identity was verified">
            <Select
              value={ferpaMethod}
              onChange={(e) => setFerpaMethod(e.target.value)}
            >
              <option value="in_person_photo_id">In-person photo identification</option>
              <option value="notarized_statement">Notarized written statement</option>
              <option value="district_records_match">Match against district student records</option>
              <option value="court_order">Court order or custody decree</option>
            </Select>
          </Field>
          <Field
            label="What you relied on"
            hint="Required — at least 20 characters; this is your statement"
          >
            <Textarea
              rows={3}
              value={ferpaNote}
              onChange={(e) => setFerpaNote(e.target.value)}
              required
              minLength={20}
            />
          </Field>
          {verifyFerpaIdentity.isError && (
            <ErrorNote error={verifyFerpaIdentity.error} />
          )}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setVerifyingFerpa(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                verifyFerpaIdentity.isPending || ferpaNote.trim().length < 20
              }
            >
              {verifyFerpaIdentity.isPending ? 'Recording…' : 'Record verification'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={canManageRequest && !terminalRequest && attesting}
        title="Record a response sent outside this system"
        onClose={() => setAttesting(false)}
      >
        <form onSubmit={submitAttestation} className="space-y-4">
          <p className="text-sm text-ink-muted">
            This records your statement that the five-day response already
            reached the requester through a channel this system did not
            deliver. It sends nothing and starts no clock. The channel, the
            date if known, and where a copy can be found go on the timeline
            and the audit trail under your name.
          </p>
          <Field label="Sent through">
            <Select value={attestVia} onChange={(e) => setAttestVia(e.target.value)}>
              <option value="nextrequest">NextRequest (retired system)</option>
              <option value="district_email">District email outside this system</option>
              <option value="mail">Paper letter or mail</option>
              <option value="other">Other channel</option>
            </Select>
          </Field>
          <Field label="Date sent" hint="Leave blank if the date is not on record">
            <Input
              type="date"
              value={attestDate}
              onChange={(e) => setAttestDate(e.target.value)}
            />
          </Field>
          <Field
            label="What was sent and where a copy can be found"
            hint="Required — at least 20 characters; this is your statement"
          >
            <Textarea
              rows={3}
              value={attestEvidence}
              onChange={(e) => setAttestEvidence(e.target.value)}
              required
              minLength={20}
            />
          </Field>
          {attestResponse.isError && <ErrorNote error={attestResponse.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setAttesting(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={attestResponse.isPending || attestEvidence.trim().length < 20}
            >
              {attestResponse.isPending ? 'Recording…' : 'Record response'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={canManageRequest && !terminalRequest && merging}
        title="Merge into case"
        onClose={() => setMerging(false)}
      >
        <form onSubmit={submitMerge} className="space-y-4">
          <p className="text-sm text-ink-muted">
            Every requester email on this case moves to the case you name, this
            case closes as Closed – Duplicate with a link to it, and the merge
            is recorded on both timelines. The case you name keeps its own
            received date and five-day clock, so name the case that received
            the request first.
          </p>
          <Field label="Merge into case number" hint="For example PRR-2026-0048">
            <Input
              value={mergeTarget}
              onChange={(e) => setMergeTarget(e.target.value)}
              required
            />
          </Field>
          <Field
            label="Reason"
            hint={reasonHint('say why these two numbers are one request')}
          >
            <Textarea
              rows={3}
              value={mergeReason}
              onChange={(e) => setMergeReason(e.target.value)}
              required
              minLength={10}
            />
          </Field>
          {merge.isError && <ErrorNote error={merge.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setMerging(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                merge.isPending
                || mergeTarget.trim().length === 0
                || reasonTooShort(mergeReason)
              }
            >
              {merge.isPending ? 'Merging…' : 'Merge into case'}
            </Button>
          </div>
        </form>
      </Modal>

    </>
  )
}
