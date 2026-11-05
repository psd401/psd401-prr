// ABOUTME: Installment production, adopted fees, payment evidence, and release.
// ABOUTME: Every legal action uses an attributed staff API domain primitive.
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { StaffRole } from '../api/auth'
import { Link } from 'react-router'
import { api, ApiError, unwrap, type Page } from '../api/client'
import { caseTabPath, stageLabel } from '../nav'
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
  MIN_REASON_LENGTH,
  reasonTooShort,
} from '../ui'

interface FeeSchedule {
  id: string
  name: string
  basis: 'statutory_default' | 'actual_cost'
  effective_on: string
  counsel_approval_reference: string
  adoption_reference: string
  photocopy_page_rate: string
  scanned_page_rate: string
  electronic_file_bundle_rate: string
  electronic_gb_rate: string
  flat_fee_amount: string | null
  is_current: boolean
}

interface FeeAssessment {
  id: string
  revision: number
  amount: string
  deposit_amount: string
  reason: string
  waiver_floor_amount: string | null
  below_waiver_floor: boolean
}

interface FeeWaiverFloor {
  id: string
  amount: string
  counsel_reference: string
  effective_on: string
  is_current: boolean
}

interface FeeDepositGate {
  id: string
  request_number: string
  scope: 'collection' | 'production'
  amount: string
  estimated_total: string
  reason: string
  requested_on: string
  satisfied_at: string | null
  payment_amount: string | null
  payment_reference: string
  lifted_at: string | null
  lift_reason: string
  is_open: boolean
}

const DEPOSIT_SCOPE_LABELS: Record<FeeDepositGate['scope'], string> = {
  collection: 'Collection (searching and exporting)',
  production: 'Production (packaging and release)',
}

interface Installment {
  id: number
  request_number: string
  sequence: number
  fee_amount: string
  fee_paid_at: string | null
  released_at: string | null
  download_expires_at: string | null
  item_count: number
  package_prepared_at: string | null
  package_prepared_by_username: string | null
  package_size: number | null
  has_exemptions: boolean
  exemption_log_ready: boolean
  exemption_log_prepared_at: string | null
  exemption_log_prepared_by_username: string | null
  fee_assessment?: FeeAssessment | null
  fee_paid_amount?: string
  fee_balance?: string
  download_count: number
  last_downloaded_at: string | null
  requires_access_code: boolean
  access_code_set_at: string | null
  revoked_at: string | null
  revoked_by_username: string | null
  revocation_reason: string
  record_item_ids: string[]
  contents_locked: boolean
}

interface ReleasedInstallment extends Installment {
  download_url: string
  correspondence_id: string
  correspondence_status: string
}

/** One record the Releases tab may put into a new installment. */
interface ReleasableRecord {
  id: string
  original_filename: string
  custodian: string
  review_state: string
  content_length: number
  redaction_count: number
}

function stateBadge(row: Installment) {
  if (row.revoked_at) return <Badge tone="red">Revoked</Badge>
  if (row.has_exemptions && !row.exemption_log_ready) {
    return <Badge tone="amber">Needs exemption log</Badge>
  }
  if (row.package_prepared_at === null) return <Badge tone="amber">Needs package</Badge>
  if (row.released_at) return <Badge tone="green">Released</Badge>
  if (row.fee_paid_at) return <Badge tone="blue">Fee paid</Badge>
  if (Number(row.fee_amount) === 0) return <Badge tone="blue">Ready for release — no fee</Badge>
  return <Badge tone="amber">Awaiting payment</Badge>
}

/** Fee panels lead when a fee applies, and fold away when it is $0 or waived. */
function FeePanels({ open, children }: { open: boolean; children: React.ReactNode }) {
  if (open) return <>{children}</>
  return (
    <details className="mb-5">
      <summary className="cursor-pointer text-sm font-semibold text-ink">
        Fees and deposits — no fee applies
      </summary>
      <div className="mt-3">{children}</div>
    </details>
  )
}

const EMPTY_ASSESSMENT_FORM = {
  photocopy_pages: '0',
  scanned_pages: '0',
  electronic_files: '0',
  electronic_gb: '0',
  media_cost: '0',
  postage_cost: '0',
  customized_service_cost: '0',
  customized_service_explanation: '',
  customized_service_expertise: '',
  requester_notice_reference: '',
  requester_approval_reference: '',
  deposit_percent: '0',
  use_flat_fee: false,
  waiver_amount: '0',
  waiver_reason: '',
  reason: '',
}

export function Installments({
  roles = [],
  requestNumber,
}: {
  roles?: StaffRole[]
  requestNumber?: string
}) {
  const qc = useQueryClient()
  const [preparing, setPreparing] = useState<Installment | null>(null)
  const [generatingLog, setGeneratingLog] = useState<Installment | null>(null)
  const [releasing, setReleasing] = useState<Installment | null>(null)
  const [releasedLink, setReleasedLink] = useState('')
  const [releasedNoticeId, setReleasedNoticeId] = useState('')
  const [isFinal, setIsFinal] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [revoking, setRevoking] = useState<Installment | null>(null)
  const [revokeReason, setRevokeReason] = useState('')
  const [codingAccess, setCodingAccess] = useState<Installment | null>(null)
  const [accessCode, setAccessCode] = useState('')
  const [editingContents, setEditingContents] = useState<Installment | null>(null)
  const [editedItems, setEditedItems] = useState<string[]>([])
  const [activatingSchedule, setActivatingSchedule] = useState(false)
  const [assessing, setAssessing] = useState<Installment | null>(null)
  const [recordingPayment, setRecordingPayment] = useState<Installment | null>(null)
  const [activatingFloor, setActivatingFloor] = useState(false)
  const [applyingGate, setApplyingGate] = useState(false)
  const [payingGate, setPayingGate] = useState<FeeDepositGate | null>(null)
  const [liftingGate, setLiftingGate] = useState<FeeDepositGate | null>(null)
  const [gateLiftReason, setGateLiftReason] = useState('')
  const [scheduleForm, setScheduleForm] = useState({
    name: '',
    basis: 'statutory_default' as FeeSchedule['basis'],
    effective_on: '',
    counsel_approval_reference: '',
    adoption_reference: '',
    actual_cost_statement_reference: '',
    photocopy_page_rate: '0.15',
    scanned_page_rate: '0.10',
    electronic_file_bundle_rate: '0.05',
    electronic_gb_rate: '0.10',
    flat_fee_amount: '2.00',
  })
  const [floorForm, setFloorForm] = useState({
    amount: '1.00',
    counsel_reference: '',
    effective_on: '',
  })
  const [gateForm, setGateForm] = useState({
    scope: 'production' as FeeDepositGate['scope'],
    amount: '',
    estimated_total: '',
    reason: '',
  })
  const [gatePaymentForm, setGatePaymentForm] = useState({
    amount: '',
    method: 'check',
    reference: '',
    received_on: new Date().toISOString().slice(0, 10),
  })
  const [assessmentForm, setAssessmentForm] = useState(EMPTY_ASSESSMENT_FORM)
  const [paymentForm, setPaymentForm] = useState({
    amount: '',
    method: 'check',
    reference: '',
    received_on: new Date().toISOString().slice(0, 10),
  })
  const canPrepare = roles.includes('records_officer')
  const list = useQuery({
    queryKey: ['installments', requestNumber ?? 'all'],
    queryFn: () => api<Page<Installment> | Installment[]>(
      requestNumber
        ? `/api/staff/installments/?request=${encodeURIComponent(requestNumber)}`
        : '/api/staff/installments/',
    ),
  })
  // The ready-to-release list. Both filters are derived server-side: an
  // officer never has to work out which records are eligible, and the list
  // they select from is the same set the packager will accept.
  const readyRecords = useQuery({
    enabled: Boolean(requestNumber),
    queryKey: ['release-ready-records', requestNumber ?? ''],
    queryFn: () => api<Page<ReleasableRecord> | ReleasableRecord[]>(
      `/api/staff/record-items/?request=${encodeURIComponent(requestNumber ?? '')}`
      + '&release_ready=true&unreleased=true',
    ),
  })
  // Every releasable record on the case, claimed or not. Editing an open
  // installment's contents needs the records it already holds as well as the
  // ones still free, and only this query can name them.
  const allReadyRecords = useQuery({
    enabled: Boolean(requestNumber),
    queryKey: ['release-ready-records-all', requestNumber ?? ''],
    queryFn: () => api<Page<ReleasableRecord> | ReleasableRecord[]>(
      `/api/staff/record-items/?request=${encodeURIComponent(requestNumber ?? '')}`
      + '&release_ready=true',
    ),
  })
  const schedules = useQuery({
    queryKey: ['fee-schedules'],
    queryFn: () => api<Page<FeeSchedule> | FeeSchedule[]>('/api/staff/fee-schedules/'),
  })
  const currentSchedule = schedules.data
    ? unwrap(schedules.data).find((schedule) => schedule.is_current)
    : undefined
  const waiverFloors = useQuery({
    queryKey: ['fee-waiver-floors'],
    queryFn: () => api<Page<FeeWaiverFloor> | FeeWaiverFloor[]>(
      '/api/staff/fee-waiver-floors/',
    ),
  })
  const currentFloor = waiverFloors.data
    ? unwrap(waiverFloors.data).find((floor) => floor.is_current)
    : undefined
  const depositGates = useQuery({
    enabled: Boolean(requestNumber),
    queryKey: ['fee-deposit-gates', requestNumber ?? ''],
    queryFn: () => api<Page<FeeDepositGate> | FeeDepositGate[]>(
      `/api/staff/fee-deposit-gates/?request=${encodeURIComponent(requestNumber ?? '')}`,
    ),
  })
  const gateRows = depositGates.data ? unwrap(depositGates.data) : []
  const openGates = gateRows.filter((gate) => gate.is_open)
  const openProductionGate = openGates.find(
    (gate) => gate.scope === 'production',
  )

  function invalidateCaseReadModels() {
    if (!requestNumber) return
    qc.invalidateQueries({ queryKey: ['case-workspace-summary', requestNumber] })
    qc.invalidateQueries({ queryKey: ['case-closure-readiness', requestNumber] })
  }


  /** Every installment action refreshes the same reads afterwards. */
  async function refreshReleaseState() {
    await qc.invalidateQueries({ queryKey: ['installments'] })
    await qc.invalidateQueries({ queryKey: ['release-ready-records'] })
    await qc.invalidateQueries({ queryKey: ['release-ready-records-all'] })
    invalidateCaseReadModels()
  }
  const prepare = useMutation({
    mutationFn: (id: number) => api<Installment>(
      `/api/staff/installments/${id}/prepare/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: async () => {
      setPreparing(null)
      await refreshReleaseState()
    },
  })
  const generateLog = useMutation({
    mutationFn: (id: number) => api<Installment>(
      `/api/staff/installments/${id}/generate-exemption-log/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: async () => {
      setGeneratingLog(null)
      await refreshReleaseState()
    },
  })
  const createInstallment = useMutation({
    mutationFn: () => api<Installment>('/api/staff/installments/', {
      method: 'POST',
      body: {
        request_number: requestNumber,
        record_item_ids: selected,
      },
    }),
    onSuccess: async () => {
      setSelected([])
      await refreshReleaseState()
    },
  })
  // Build package (#47): create, exemption log, and prepare in one server
  // action. It never releases; a blocker names the case tab that fixes it.
  const buildPackage = useMutation({
    mutationFn: () => api<Installment>(
      `/api/staff/requests/${requestNumber}/release-package/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: async () => {
      await refreshReleaseState()
    },
  })
  // One action, because it was always one decision. The server releases the
  // package, drafts the notice from the installment, and sends it under the
  // same officer's name; nothing here is automated in the rule 6 sense.
  const release = useMutation({
    mutationFn: (id: number) => api<ReleasedInstallment>(
      `/api/staff/installments/${id}/release-and-send/`,
      { method: 'POST', body: { is_final: isFinal } },
    ),
    onSuccess: async (result) => {
      setReleasedLink(result.download_url)
      setReleasedNoticeId(result.correspondence_id)
      await refreshReleaseState()
    },
    // A 409 means the release stands with its notice still a draft. The
    // message says so; the row must show it released rather than pending.
    onError: async () => {
      await refreshReleaseState()
    },
  })
  const setItems = useMutation({
    mutationFn: (id: number) => api<Installment>(
      `/api/staff/installments/${id}/set-items/`,
      { method: 'POST', body: { record_item_ids: editedItems } },
    ),
    onSuccess: async () => {
      setEditingContents(null)
      setEditedItems([])
      await refreshReleaseState()
    },
  })
  const revoke = useMutation({
    mutationFn: (id: number) => api<Installment>(
      `/api/staff/installments/${id}/revoke/`,
      { method: 'POST', body: { reason: revokeReason } },
    ),
    onSuccess: async () => {
      setRevoking(null)
      setRevokeReason('')
      await refreshReleaseState()
    },
  })
  function closeAccessCode() {
    // The code is a secret typed in the clear; nothing keeps it after the
    // dialog closes, whether or not it was saved.
    setCodingAccess(null)
    setAccessCode('')
  }
  const setCode = useMutation({
    mutationFn: (id: number) => api<Installment>(
      `/api/staff/installments/${id}/set-access-code/`,
      { method: 'POST', body: { access_code: accessCode } },
    ),
    onSuccess: async () => {
      closeAccessCode()
      await refreshReleaseState()
    },
  })
  const clearCode = useMutation({
    mutationFn: (id: number) => api<Installment>(
      `/api/staff/installments/${id}/clear-access-code/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: async () => {
      closeAccessCode()
      await refreshReleaseState()
    },
  })
  // The adopted schedule is entered, not assumed. A district that adopts an
  // actual-cost schedule or a rate below the statutory default has to be able
  // to say so; the server still enforces the RCW 42.56.120 caps.
  const activateSchedule = useMutation({
    mutationFn: () => api<FeeSchedule>('/api/staff/fee-schedules/', {
      method: 'POST',
      body: {
        ...scheduleForm,
        flat_fee_amount: scheduleForm.flat_fee_amount || null,
      },
    }),
    onSuccess: async () => {
      setActivatingSchedule(false)
      await qc.invalidateQueries({ queryKey: ['fee-schedules'] })
    },
  })
  const activateFloor = useMutation({
    mutationFn: () => api<FeeWaiverFloor>('/api/staff/fee-waiver-floors/', {
      method: 'POST',
      body: floorForm,
    }),
    onSuccess: async () => {
      setActivatingFloor(false)
      await qc.invalidateQueries({ queryKey: ['fee-waiver-floors'] })
      await qc.invalidateQueries({ queryKey: ['installments'] })
    },
  })
  async function refreshDepositGates() {
    await qc.invalidateQueries({ queryKey: ['fee-deposit-gates'] })
    await refreshReleaseState()
  }
  const applyGate = useMutation({
    mutationFn: () => api<FeeDepositGate>('/api/staff/fee-deposit-gates/', {
      method: 'POST',
      body: { ...gateForm, request_number: requestNumber },
    }),
    onSuccess: async () => {
      setApplyingGate(false)
      await refreshDepositGates()
    },
  })
  const recordGatePayment = useMutation({
    mutationFn: (gate: FeeDepositGate) => api<FeeDepositGate>(
      `/api/staff/fee-deposit-gates/${gate.id}/record-payment/`,
      { method: 'POST', body: gatePaymentForm },
    ),
    onSuccess: async () => {
      setPayingGate(null)
      await refreshDepositGates()
    },
  })
  const liftGate = useMutation({
    mutationFn: (gate: FeeDepositGate) => api<FeeDepositGate>(
      `/api/staff/fee-deposit-gates/${gate.id}/lift/`,
      { method: 'POST', body: { reason: gateLiftReason } },
    ),
    onSuccess: async () => {
      setLiftingGate(null)
      setGateLiftReason('')
      await refreshDepositGates()
    },
  })
  const assessFee = useMutation({
    mutationFn: (installment: Installment) => api<Installment>(
      `/api/staff/installments/${installment.id}/assess-fee/`,
      {
        method: 'POST',
        body: {
          ...assessmentForm,
          schedule_id: currentSchedule?.id,
        },
      },
    ),
    onSuccess: async () => {
      setAssessing(null)
      await refreshReleaseState()
    },
  })
  const recordPayment = useMutation({
    mutationFn: (installment: Installment) => api<Installment>(
      `/api/staff/installments/${installment.id}/record-payment/`,
      { method: 'POST', body: paymentForm },
    ),
    onSuccess: async () => {
      setRecordingPayment(null)
      await refreshReleaseState()
    },
  })

  if (list.isPending || schedules.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />

  const rows = unwrap(list.data)
  const ready = readyRecords.data ? unwrap(readyRecords.data) : []
  const allReady = allReadyRecords.data ? unwrap(allReadyRecords.data) : []
  const allSelected = ready.length > 0 && selected.length === ready.length
  const unclaimedIds = new Set(ready.map((row) => row.id))
  // Candidates for an open installment: what it already holds, plus whatever
  // no other live installment has claimed.
  const editCandidates = editingContents
    ? allReady.filter((row) => (
      editingContents.record_item_ids.includes(row.id) || unclaimedIds.has(row.id)
    ))
    : []

  // The package the one-step flow works on: the newest installment that is
  // neither released nor revoked. Build package finishes it before opening
  // another.
  const openPackage = rows
    .filter((row) => !row.released_at && !row.revoked_at)
    .sort((a, b) => b.sequence - a.sequence)[0]
  const packageReady = Boolean(openPackage?.package_prepared_at)
  const openBalance = openPackage ? Number(openPackage.fee_balance ?? openPackage.fee_amount) : 0
  const awaitingPayment = packageReady && openBalance > 0 && !openPackage?.fee_paid_at
  // Build package finishes an open installment as the officer chose it, so
  // say plainly how many ready records it leaves for the next package.
  const leftOut = openPackage
    ? ready.filter((row) => !openPackage.record_item_ids.includes(row.id)).length
    : 0
  const packageRecords = openPackage
    ? allReady.filter((row) => openPackage.record_item_ids.includes(row.id))
    : []
  // Fee, waiver-floor, and deposit panels lead only when a fee applies: an
  // unpaid balance or an open deposit. A $0 or waived fee folds them away.
  const feeApplies = !requestNumber || openGates.length > 0 || rows.some((row) => (
    !row.revoked_at && !row.released_at && !row.fee_paid_at
    && Number(row.fee_balance ?? row.fee_amount) > 0
  ))
  const buildBlocker = buildPackage.error instanceof ApiError
    ? (buildPackage.error.data as { blocker?: { code: string; tab: string } } | null)?.blocker
    : undefined

  function toggleRecord(id: string) {
    setSelected((current) => (
      current.includes(id)
        ? current.filter((value) => value !== id)
        : [...current, id]
    ))
  }

  return (
    <>
      {requestNumber && canPrepare && (
        <Card title="Release package" className="mb-5">
          <ol className="space-y-5" aria-label="Release steps">
            <li>
              <p className="font-heading text-sm font-semibold text-ink">1. Build package</p>
              <p className="mt-1 text-sm text-ink-muted">
                {openPackage
                  ? `Installment ${openPackage.sequence}: ${openPackage.item_count} record${openPackage.item_count === 1 ? '' : 's'}${packageReady ? ', package prepared.' : ', not prepared yet.'}`
                  : `${ready.length} record${ready.length === 1 ? '' : 's'} ready to release.`}{' '}
                {!openPackage && 'Builds an installment from every ready record, generates the exemption log when one is needed, and prepares the package. Nothing is sent.'}
                {openPackage && !packageReady && 'Build package finishes this installment: it generates the exemption log when one is needed and prepares the package. Nothing is sent.'}
              </p>
              {leftOut > 0 && (
                <p className="mt-1 text-sm font-semibold text-warning-ink">
                  {leftOut} more ready record{leftOut === 1 ? ' is' : 's are'} not in this
                  installment; build again after releasing it to package the rest.
                </p>
              )}
              {!packageReady && (
                <Button
                  type="button"
                  className="mt-2"
                  disabled={buildPackage.isPending}
                  onClick={() => {
                    buildPackage.reset()
                    buildPackage.mutate()
                  }}
                >
                  {buildPackage.isPending ? 'Building…' : 'Build package'}
                </Button>
              )}
              {buildPackage.isError && (
                <div className="mt-2 space-y-1">
                  <ErrorNote error={buildPackage.error} />
                  {buildBlocker && buildBlocker.tab !== 'releases' && (
                    <Link
                      className="text-sm font-semibold text-accent underline-offset-2 hover:underline"
                      to={caseTabPath(requestNumber, buildBlocker.tab)}
                    >
                      Go to {stageLabel(buildBlocker.tab)} to fix this
                    </Link>
                  )}
                </div>
              )}
            </li>
            {openPackage && packageReady && (
              <li>
                <p className="font-heading text-sm font-semibold text-ink">2. Preview</p>
                <p className="mt-1 text-sm text-ink-muted">
                  {openPackage.has_exemptions
                    ? 'The exemption log is included.'
                    : 'No exemption log is needed.'}{' '}
                  {openPackage.package_size !== null && `Package size ${openPackage.package_size.toLocaleString()} bytes. `}
                  {openBalance > 0 && !openPackage.fee_paid_at
                    ? `Fee due: $${openBalance.toFixed(2)}.`
                    : 'No fee is due.'}
                </p>
                <ul className="mt-2 space-y-1 text-sm text-ink" aria-label="Records in this package">
                  {packageRecords.map((record) => (
                    <li key={record.id}>
                      {record.original_filename || record.id}
                      {record.review_state === 'withheld' && ' — withheld, listed in the exemption log'}
                      {record.redaction_count > 0 && ` — ${record.redaction_count} redaction${record.redaction_count === 1 ? '' : 's'}`}
                    </li>
                  ))}
                </ul>
              </li>
            )}
            {openPackage && packageReady && (
              <li>
                <p className="font-heading text-sm font-semibold text-ink">3. Release and send</p>
                {awaitingPayment ? (
                  <p className="mt-1 text-sm font-semibold text-warning-ink">
                    Record the fee payment before releasing.
                  </p>
                ) : (
                  <Button
                    type="button"
                    className="mt-2"
                    onClick={() => {
                      release.reset()
                      setReleasedLink('')
                      setReleasedNoticeId('')
                      setIsFinal(false)
                      setReleasing(openPackage)
                    }}
                  >
                    Release and send
                  </Button>
                )}
              </li>
            )}
          </ol>
        </Card>
      )}
      {requestNumber && canPrepare && (
        <details className="mb-5">
          <summary className="cursor-pointer text-sm font-semibold text-ink">
            Choose records for a partial installment
          </summary>
          <Card
            title="Ready to release"
            actions={ready.length > 0 ? (
              <div className="flex items-center gap-3">
                <label className="flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked ? ready.map((row) => row.id) : [],
                      )
                    }
                  />
                  Select all {ready.length}
                </label>
                <Button
                  type="button"
                  disabled={selected.length === 0 || createInstallment.isPending}
                  onClick={() => {
                    createInstallment.reset()
                    createInstallment.mutate()
                  }}
                >
                  {createInstallment.isPending
                    ? 'Creating…'
                    : `Create installment (${selected.length})`}
                </Button>
              </div>
            ) : undefined}
            className="mb-5"
          >
            {readyRecords.isPending ? (
              <Spinner label="Loading releasable records…" />
            ) : readyRecords.isError ? (
              <ErrorNote error={readyRecords.error} />
            ) : ready.length === 0 ? (
              <EmptyState>
                No records are ready to release. A record becomes ready once it
                has been reviewed, any heightened-review citation has a second
                approval, and any redactions have been rendered.
              </EmptyState>
            ) : (
              <>
                {createInstallment.isError && (
                  <ErrorNote error={createInstallment.error} />
                )}
                <Table
                  columns={[
                    {
                      header: 'Include',
                      cell: (r: ReleasableRecord) => (
                        <input
                          type="checkbox"
                          aria-label={`Include ${r.original_filename}`}
                          checked={selected.includes(r.id)}
                          onChange={() => toggleRecord(r.id)}
                        />
                      ),
                    },
                    {
                      header: 'Record',
                      cell: (r: ReleasableRecord) => r.original_filename || r.id,
                    },
                    {
                      header: 'Custodian',
                      cell: (r: ReleasableRecord) => r.custodian || '—',
                    },
                    {
                      header: 'Review',
                      cell: (r: ReleasableRecord) => (
                        r.review_state === 'withheld'
                          ? <Badge tone="amber">Withheld</Badge>
                          : <Badge tone="green">Responsive</Badge>
                      ),
                    },
                    {
                      header: 'Redactions',
                      align: 'right' as const,
                      cell: (r: ReleasableRecord) => r.redaction_count,
                    },
                  ]}
                  rows={ready}
                  rowKey={(r) => r.id}
                />
              </>
            )}
          </Card>
        </details>
      )}
      <FeePanels open={feeApplies}>
        <Card
          title="Adopted fee schedule"
          actions={canPrepare && !currentSchedule ? (
            <Button type="button" onClick={() => {
              activateSchedule.reset()
              setActivatingSchedule(true)
            }}>
              Activate fee schedule
            </Button>
          ) : undefined}
          className="mb-5"
        >
          {schedules.isError ? (
            <ErrorNote error={schedules.error} />
          ) : currentSchedule ? (
            <div className="space-y-2 text-sm text-ink">
              <p>
                <span className="font-heading font-semibold">{currentSchedule.name}</span>{' '}
                <Badge tone="green">Effective {currentSchedule.effective_on}</Badge>
              </p>
              <p>Statutory schedule: $0.15 photocopy page, $0.10 scanned page, $0.05 per four electronic files, and $0.10/GB.</p>
              <p className="text-xs text-ink-muted">
                Counsel: {currentSchedule.counsel_approval_reference} · Adoption: {currentSchedule.adoption_reference}
              </p>
            </div>
          ) : (
            <p className="text-sm text-ink-muted">
              No fee schedule is active. Do not assess or communicate a fee until counsel approval and formal district adoption are recorded.
            </p>
          )}
        </Card>
        <Card
          title="Fee waiver floor"
          actions={canPrepare ? (
            <Button type="button" variant="ghost" onClick={() => {
              activateFloor.reset()
              setActivatingFloor(true)
            }}>
              {currentFloor ? 'Replace floor' : 'Adopt waiver floor'}
            </Button>
          ) : undefined}
          className="mb-5"
        >
          {waiverFloors.isError ? (
            <ErrorNote error={waiverFloors.error} />
          ) : currentFloor ? (
            <div className="space-y-1 text-sm text-ink">
              <p>
                Assessments under{' '}
                <span className="font-heading font-semibold">${currentFloor.amount}</span>{' '}
                prompt a waiver. Nothing is waived automatically; a Records Officer
                records the waiver and its reason on the assessment.
              </p>
              <p className="text-xs text-ink-muted">
                Counsel: {currentFloor.counsel_reference} · Effective {currentFloor.effective_on}
              </p>
            </div>
          ) : (
            <p className="text-sm text-ink-muted">
              No waiver floor is adopted, so no assessment will prompt a waiver.
            </p>
          )}
        </Card>
        {requestNumber && (
          <Card
            title="Deposit gates"
            actions={canPrepare ? (
              <Button type="button" variant="ghost" onClick={() => {
                applyGate.reset()
                setApplyingGate(true)
              }}>
                Require a deposit
              </Button>
            ) : undefined}
            className="mb-5"
          >
            {depositGates.isError ? (
              <ErrorNote error={depositGates.error} />
            ) : gateRows.length === 0 ? (
              <p className="text-sm text-ink-muted">
                No deposit is holding work on this case. A deposit gate stops
                collection or production until the requester pays; it never closes
                the case and never expires.
              </p>
            ) : (
              <Table
                columns={[
                  {
                    header: 'Work held',
                    cell: (g: FeeDepositGate) => DEPOSIT_SCOPE_LABELS[g.scope],
                  },
                  { header: 'Deposit', cell: (g: FeeDepositGate) => `$${g.amount}`, align: 'right' },
                  {
                    header: 'Of estimate',
                    cell: (g: FeeDepositGate) => `$${g.estimated_total}`,
                    align: 'right',
                  },
                  { header: 'Requested', cell: (g: FeeDepositGate) => g.requested_on },
                  {
                    header: 'State',
                    cell: (g: FeeDepositGate) => (
                      g.satisfied_at
                        ? <Badge tone="green">Deposit paid</Badge>
                        : g.lifted_at
                          ? <Badge tone="blue">Lifted</Badge>
                          : <Badge tone="amber">Holding work</Badge>
                    ),
                  },
                  {
                    header: 'Actions',
                    cell: (g: FeeDepositGate) => (canPrepare && g.is_open ? (
                      <div className="flex gap-2">
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => {
                            recordGatePayment.reset()
                            setGatePaymentForm({
                              amount: g.amount,
                              method: 'check',
                              reference: '',
                              received_on: new Date().toISOString().slice(0, 10),
                            })
                            setPayingGate(g)
                          }}
                        >
                          Record deposit
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => {
                            liftGate.reset()
                            setGateLiftReason('')
                            setLiftingGate(g)
                          }}
                        >
                          Lift
                        </Button>
                      </div>
                    ) : <span className="text-xs text-ink-muted">—</span>),
                  },
                ]}
                rows={gateRows}
                rowKey={(g) => g.id}
              />
            )}
          </Card>
        )}
      </FeePanels>
      <Card title="Installments and releases">
        {rows.length === 0 ? (
          <EmptyState>Installments appear when records are released in batches.</EmptyState>
        ) : (
          <Table
            columns={[
              { header: 'Sequence', cell: (r: Installment) => r.sequence, align: 'right' },
              { header: 'Items', cell: (r: Installment) => r.item_count, align: 'right' },
              { header: 'Fee amount', cell: (r: Installment) => r.fee_amount, align: 'right' },
              { header: 'Fee paid', cell: (r: Installment) => r.fee_paid_at?.slice(0, 10) ?? '—' },
              { header: 'Released', cell: (r: Installment) => r.released_at?.slice(0, 10) ?? '—' },
              {
                header: 'Download expires',
                cell: (r: Installment) => r.download_expires_at?.slice(0, 10) ?? '—',
              },
              {
                header: 'Downloads',
                align: 'right' as const,
                cell: (r: Installment) => r.download_count,
              },
              {
                header: 'Access code',
                cell: (r: Installment) => (
                  r.requires_access_code
                    ? <Badge tone="blue">Required</Badge>
                    : '—'
                ),
              },
              { header: 'State', cell: (r: Installment) => stateBadge(r) },
              ...(canPrepare ? [{
                header: 'Action',
                cell: (r: Installment) => {
                  if (r.revoked_at) return 'Revoked'
                  if (r.released_at) {
                    return (
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => {
                          revoke.reset()
                          setRevokeReason('')
                          setRevoking(r)
                        }}
                      >
                        Revoke download
                      </Button>
                    )
                  }
                  const feeBalance = Number(r.fee_balance ?? r.fee_amount)
                  return (
                    <div className="flex flex-wrap gap-2">
                      {currentSchedule && (
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => {
                            assessFee.reset()
                            setAssessmentForm(EMPTY_ASSESSMENT_FORM)
                            setAssessing(r)
                          }}
                        >
                          Assess fee
                        </Button>
                      )}
                      {r.fee_assessment && feeBalance > 0 && !r.fee_paid_at && (
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => {
                            recordPayment.reset()
                            setPaymentForm({
                              amount: r.fee_balance ?? r.fee_amount,
                              method: 'check',
                              reference: '',
                              received_on: new Date().toISOString().slice(0, 10),
                            })
                            setRecordingPayment(r)
                          }}
                        >
                          Record payment
                        </Button>
                      )}
                      {!r.contents_locked && (
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => {
                            setItems.reset()
                            setEditedItems(r.record_item_ids)
                            setEditingContents(r)
                          }}
                        >
                          Edit contents
                        </Button>
                      )}
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => {
                          setCode.reset()
                          clearCode.reset()
                          setAccessCode('')
                          setCodingAccess(r)
                        }}
                      >
                        {r.requires_access_code ? 'Change access code' : 'Add access code'}
                      </Button>
                      {r.fee_assessment?.below_waiver_floor && !r.fee_paid_at && (
                        <span className="self-center text-xs font-semibold text-warning-ink">
                          Under the ${r.fee_assessment.waiver_floor_amount} waiver
                          floor — waive it or collect it
                        </span>
                      )}
                      {openProductionGate ? (
                        <span className="self-center text-xs font-semibold text-warning-ink">
                          ${openProductionGate.amount} deposit required before
                          production
                        </span>
                      ) : r.has_exemptions && !r.exemption_log_ready ? (
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => {
                          generateLog.reset()
                          setGeneratingLog(r)
                        }}
                      >
                        Generate exemption log
                      </Button>
                      ) : r.package_prepared_at === null ? (
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => {
                          prepare.reset()
                          setPreparing(r)
                        }}
                      >
                        Prepare package
                      </Button>
                      ) : feeBalance > 0 && !r.fee_paid_at ? (
                        <span className="self-center text-xs font-semibold text-warning-ink">Payment required</span>
                      ) : (
                        <Button
                          type="button"
                          onClick={() => {
                            release.reset()
                            setReleasedLink('')
                            setReleasedNoticeId('')
                            setIsFinal(false)
                            setReleasing(r)
                          }}
                        >
                          Release and send
                        </Button>
                      )}
                    </div>
                  )
                },
              }] : []),
            ]}
            rows={rows}
            rowKey={(r) => r.id}
          />
        )}
      </Card>
      <Modal
        open={canPrepare && activatingSchedule}
        title="Activate counsel-approved fee schedule"
        onClose={() => setActivatingSchedule(false)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            activateSchedule.mutate()
          }}
        >
          <p className="text-sm text-ink">
            This creates an immutable schedule from the rates the district adopted. Activate it only after district counsel approval and formal adoption; the application does not supply either approval, and it will refuse a statutory-default rate above the RCW 42.56.120 cap.
          </p>
          <Field label="Basis">
            <Select
              aria-label="Basis"
              value={scheduleForm.basis}
              onChange={(event) => setScheduleForm({
                ...scheduleForm,
                basis: event.target.value as FeeSchedule['basis'],
              })}
            >
              <option value="statutory_default">Statutory default rates</option>
              <option value="actual_cost">Published actual cost</option>
            </Select>
          </Field>
          <Field label="Schedule name">
            <Input
              required
              aria-label="Schedule name"
              value={scheduleForm.name}
              onChange={(event) => setScheduleForm({ ...scheduleForm, name: event.target.value })}
            />
          </Field>
          <Field label="Effective date">
            <Input
              required
              aria-label="Effective date"
              type="date"
              value={scheduleForm.effective_on}
              onChange={(event) => setScheduleForm({ ...scheduleForm, effective_on: event.target.value })}
            />
          </Field>
          <Field label="Counsel approval reference">
            <Input
              required
              aria-label="Counsel approval reference"
              value={scheduleForm.counsel_approval_reference}
              onChange={(event) => setScheduleForm({
                ...scheduleForm,
                counsel_approval_reference: event.target.value,
              })}
            />
          </Field>
          <Field label="Formal adoption reference">
            <Input
              required
              aria-label="Formal adoption reference"
              value={scheduleForm.adoption_reference}
              onChange={(event) => setScheduleForm({
                ...scheduleForm,
                adoption_reference: event.target.value,
              })}
            />
          </Field>
          {scheduleForm.basis === 'actual_cost' && (
            <Field
              label="Published actual-cost statement reference"
              hint="RCW 42.56.120(2)(b) requires the district to publish the statement of its actual costs before charging them."
            >
              <Input
                required
                aria-label="Published actual-cost statement reference"
                value={scheduleForm.actual_cost_statement_reference}
                onChange={(event) => setScheduleForm({
                  ...scheduleForm,
                  actual_cost_statement_reference: event.target.value,
                })}
              />
            </Field>
          )}
          <div className="grid grid-cols-2 gap-3">
            <Field label="Photocopy rate per page">
              <Input
                type="number"
                min="0"
                step="0.0001"
                aria-label="Photocopy rate per page"
                value={scheduleForm.photocopy_page_rate}
                onChange={(event) => setScheduleForm({
                  ...scheduleForm,
                  photocopy_page_rate: event.target.value,
                })}
              />
            </Field>
            <Field label="Scan rate per page">
              <Input
                type="number"
                min="0"
                step="0.0001"
                aria-label="Scan rate per page"
                value={scheduleForm.scanned_page_rate}
                onChange={(event) => setScheduleForm({
                  ...scheduleForm,
                  scanned_page_rate: event.target.value,
                })}
              />
            </Field>
            <Field label="Rate per four electronic files">
              <Input
                type="number"
                min="0"
                step="0.0001"
                aria-label="Rate per four electronic files"
                value={scheduleForm.electronic_file_bundle_rate}
                onChange={(event) => setScheduleForm({
                  ...scheduleForm,
                  electronic_file_bundle_rate: event.target.value,
                })}
              />
            </Field>
            <Field label="Electronic transmission rate per GB">
              <Input
                type="number"
                min="0"
                step="0.0001"
                aria-label="Electronic transmission rate per GB"
                value={scheduleForm.electronic_gb_rate}
                onChange={(event) => setScheduleForm({
                  ...scheduleForm,
                  electronic_gb_rate: event.target.value,
                })}
              />
            </Field>
          </div>
          <Field
            label="Flat fee alternative"
            hint="Leave blank if the district did not adopt the RCW 42.56.120(2)(b) flat fee. At most $2.00."
          >
            <Input
              type="number"
              min="0"
              step="0.01"
              aria-label="Flat fee alternative"
              value={scheduleForm.flat_fee_amount}
              onChange={(event) => setScheduleForm({
                ...scheduleForm,
                flat_fee_amount: event.target.value,
              })}
            />
          </Field>
          {activateSchedule.isError && <ErrorNote error={activateSchedule.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setActivatingSchedule(false)}>Cancel</Button>
            <Button type="submit" disabled={activateSchedule.isPending}>
              {activateSchedule.isPending ? 'Activating…' : 'Activate schedule'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && activatingFloor}
        title="Adopt the fee waiver floor"
        onClose={() => setActivatingFloor(false)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            activateFloor.mutate()
          }}
        >
          <p className="text-sm text-ink">
            An assessment under this amount will prompt the officer to waive it.
            Adopting a floor waives nothing by itself and changes no existing
            assessment.
          </p>
          <Field label="Floor amount">
            <Input
              required
              type="number"
              min="0.01"
              step="0.01"
              aria-label="Floor amount"
              value={floorForm.amount}
              onChange={(event) => setFloorForm({ ...floorForm, amount: event.target.value })}
            />
          </Field>
          <Field label="Counsel reference">
            <Input
              required
              aria-label="Counsel reference"
              value={floorForm.counsel_reference}
              onChange={(event) => setFloorForm({
                ...floorForm,
                counsel_reference: event.target.value,
              })}
            />
          </Field>
          <Field label="Effective date">
            <Input
              required
              type="date"
              aria-label="Effective date"
              value={floorForm.effective_on}
              onChange={(event) => setFloorForm({
                ...floorForm,
                effective_on: event.target.value,
              })}
            />
          </Field>
          {activateFloor.isError && <ErrorNote error={activateFloor.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setActivatingFloor(false)}>Cancel</Button>
            <Button type="submit" disabled={activateFloor.isPending}>
              {activateFloor.isPending ? 'Adopting…' : 'Adopt floor'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && applyingGate}
        title="Require a deposit before this work continues"
        onClose={() => setApplyingGate(false)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            applyGate.mutate()
          }}
        >
          <p className="text-sm text-ink">
            RCW 42.56.120(2) allows a deposit of up to ten percent of the
            estimated charge. While the deposit is open the work you pick here
            refuses to run. The case stays open and nothing closes on its own.
          </p>
          <Field label="Work to hold">
            <Select
              aria-label="Work to hold"
              value={gateForm.scope}
              onChange={(event) => setGateForm({
                ...gateForm,
                scope: event.target.value as FeeDepositGate['scope'],
              })}
            >
              <option value="production">{DEPOSIT_SCOPE_LABELS.production}</option>
              <option value="collection">{DEPOSIT_SCOPE_LABELS.collection}</option>
            </Select>
          </Field>
          <Field label="Estimated charge">
            <Input
              required
              type="number"
              min="0.01"
              step="0.01"
              aria-label="Estimated charge"
              value={gateForm.estimated_total}
              onChange={(event) => setGateForm({
                ...gateForm,
                estimated_total: event.target.value,
              })}
            />
          </Field>
          <Field label="Deposit requested" hint="No more than ten percent of the estimated charge.">
            <Input
              required
              type="number"
              min="0.01"
              step="0.01"
              aria-label="Deposit requested"
              value={gateForm.amount}
              onChange={(event) => setGateForm({ ...gateForm, amount: event.target.value })}
            />
          </Field>
          <Field label="Why the District is requiring it">
            <Textarea
              required
              rows={3}
              aria-label="Why the District is requiring it"
              minLength={MIN_REASON_LENGTH}
              value={gateForm.reason}
              onChange={(event) => setGateForm({ ...gateForm, reason: event.target.value })}
            />
          </Field>
          {applyGate.isError && <ErrorNote error={applyGate.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setApplyingGate(false)}>Cancel</Button>
            <Button
              type="submit"
              disabled={applyGate.isPending || reasonTooShort(gateForm.reason)}
            >
              {applyGate.isPending ? 'Applying…' : 'Require deposit'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && payingGate !== null}
        title="Record the deposit"
        onClose={() => setPayingGate(null)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (payingGate) recordGatePayment.mutate(payingGate)
          }}
        >
          <p className="text-sm text-ink">
            Recording the full deposit releases the held work. A partial payment
            does not; lift the gate with a reason instead.
          </p>
          <Field label="Amount received">
            <Input
              required
              type="number"
              min="0.01"
              step="0.01"
              aria-label="Amount received"
              value={gatePaymentForm.amount}
              onChange={(event) => setGatePaymentForm({
                ...gatePaymentForm,
                amount: event.target.value,
              })}
            />
          </Field>
          <Field label="Method">
            <Select
              aria-label="Method"
              value={gatePaymentForm.method}
              onChange={(event) => setGatePaymentForm({
                ...gatePaymentForm,
                method: event.target.value,
              })}
            >
              <option value="check">Check</option>
              <option value="cash">Cash</option>
              <option value="card">District payment system</option>
              <option value="other">Other</option>
            </Select>
          </Field>
          <Field label="Receipt reference">
            <Input
              required
              aria-label="Receipt reference"
              value={gatePaymentForm.reference}
              onChange={(event) => setGatePaymentForm({
                ...gatePaymentForm,
                reference: event.target.value,
              })}
            />
          </Field>
          <Field label="Received on">
            <Input
              required
              type="date"
              aria-label="Received on"
              value={gatePaymentForm.received_on}
              onChange={(event) => setGatePaymentForm({
                ...gatePaymentForm,
                received_on: event.target.value,
              })}
            />
          </Field>
          {recordGatePayment.isError && <ErrorNote error={recordGatePayment.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setPayingGate(null)}>Cancel</Button>
            <Button type="submit" disabled={recordGatePayment.isPending}>
              {recordGatePayment.isPending ? 'Recording…' : 'Record deposit'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && liftingGate !== null}
        title="Lift the deposit gate"
        onClose={() => setLiftingGate(null)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (liftingGate) liftGate.mutate(liftingGate)
          }}
        >
          <p className="text-sm text-ink">
            This releases the held work without the deposit. The reason is
            recorded on the case under your name.
          </p>
          <Field label="Reason">
            <Textarea
              required
              rows={3}
              aria-label="Reason"
              minLength={MIN_REASON_LENGTH}
              value={gateLiftReason}
              onChange={(event) => setGateLiftReason(event.target.value)}
            />
          </Field>
          {liftGate.isError && <ErrorNote error={liftGate.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setLiftingGate(null)}>Cancel</Button>
            <Button
              type="submit"
              disabled={liftGate.isPending || reasonTooShort(gateLiftReason)}
            >
              {liftGate.isPending ? 'Lifting…' : 'Lift gate'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && assessing !== null}
        title="Assess installment fee"
        size="wide"
        onClose={() => setAssessing(null)}
      >
        <form
          className="space-y-5"
          onSubmit={(event) => {
            event.preventDefault()
            if (assessing) assessFee.mutate(assessing)
          }}
        >
          <p className="text-sm text-ink">
            Apply {currentSchedule?.name} to {assessing?.request_number}, installment {assessing?.sequence}. The calculation and every revision are retained as immutable evidence.
          </p>
          <div className="grid gap-4 md:grid-cols-4">
            {([
              ['Photocopy pages', 'photocopy_pages'],
              ['Scanned pages', 'scanned_pages'],
              ['Electronic files', 'electronic_files'],
              ['Electronic GB', 'electronic_gb'],
              ['Media cost', 'media_cost'],
              ['Postage cost', 'postage_cost'],
              ['Deposit percent', 'deposit_percent'],
              ['Waiver amount', 'waiver_amount'],
            ] as const).map(([label, key]) => (
              <Field
                key={key}
                label={label}
                hint={
                  key === 'deposit_percent'
                    ? 'Recorded on the assessment only. It holds no work; use Require a deposit to hold collection or production.'
                    : undefined
                }
              >
                <Input
                  aria-label={label}
                  type="number"
                  min="0"
                  max={key === 'deposit_percent' ? '10' : undefined}
                  step={key.endsWith('pages') || key === 'electronic_files' ? '1' : '0.01'}
                  value={assessmentForm[key]}
                  onChange={(event) => setAssessmentForm({
                    ...assessmentForm,
                    [key]: event.target.value,
                  })}
                />
              </Field>
            ))}
          </div>
          <label className="flex items-start gap-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={assessmentForm.use_flat_fee}
              onChange={(event) => setAssessmentForm({
                ...assessmentForm,
                use_flat_fee: event.target.checked,
              })}
            />
            <span>Use the adopted flat-fee alternative for the first installment only when the documented itemized estimate is at least that amount.</span>
          </label>
          <Field label="Waiver reason">
            <Input
              aria-label="Waiver reason"
              value={assessmentForm.waiver_reason}
              onChange={(event) => setAssessmentForm({ ...assessmentForm, waiver_reason: event.target.value })}
            />
          </Field>
          <fieldset className="space-y-3 rounded-lg border border-line p-4">
            <legend className="px-1 font-heading text-sm font-semibold text-ink">Customized service charge (only when legally applicable)</legend>
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Customized service cost">
                <Input
                  aria-label="Customized service cost"
                  type="number"
                  min="0"
                  step="0.01"
                  value={assessmentForm.customized_service_cost}
                  onChange={(event) => setAssessmentForm({ ...assessmentForm, customized_service_cost: event.target.value })}
                />
              </Field>
              <Field label="Specific expertise">
                <Input
                  aria-label="Specific expertise"
                  value={assessmentForm.customized_service_expertise}
                  onChange={(event) => setAssessmentForm({ ...assessmentForm, customized_service_expertise: event.target.value })}
                />
              </Field>
              <Field label="Requester notice reference">
                <Input
                  aria-label="Requester notice reference"
                  value={assessmentForm.requester_notice_reference}
                  onChange={(event) => setAssessmentForm({ ...assessmentForm, requester_notice_reference: event.target.value })}
                />
              </Field>
              <Field label="Requester approval reference">
                <Input
                  aria-label="Requester approval reference"
                  value={assessmentForm.requester_approval_reference}
                  onChange={(event) => setAssessmentForm({ ...assessmentForm, requester_approval_reference: event.target.value })}
                />
              </Field>
            </div>
            <Field label="Customized service explanation">
              <Textarea
                aria-label="Customized service explanation"
                rows={3}
                value={assessmentForm.customized_service_explanation}
                onChange={(event) => setAssessmentForm({
                  ...assessmentForm,
                  customized_service_explanation: event.target.value,
                })}
              />
            </Field>
          </fieldset>
          <Field label="Assessment reason">
            <Textarea
              required
              aria-label="Assessment reason"
              rows={3}
              value={assessmentForm.reason}
              onChange={(event) => setAssessmentForm({ ...assessmentForm, reason: event.target.value })}
            />
          </Field>
          {assessFee.isError && <ErrorNote error={assessFee.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setAssessing(null)}>Cancel</Button>
            <Button type="submit" disabled={assessFee.isPending}>
              {assessFee.isPending ? 'Saving…' : 'Save assessment'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && recordingPayment !== null}
        title="Record fee payment"
        onClose={() => setRecordingPayment(null)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (recordingPayment) recordPayment.mutate(recordingPayment)
          }}
        >
          <p className="text-sm text-ink">
            Record district finance receipt evidence for {recordingPayment?.request_number}. Partial payments remain outstanding; release stays blocked until the exact assessed total is recorded.
          </p>
          <Field label="Amount received">
            <Input
              required
              aria-label="Amount received"
              type="number"
              min="0.01"
              step="0.01"
              value={paymentForm.amount}
              onChange={(event) => setPaymentForm({ ...paymentForm, amount: event.target.value })}
            />
          </Field>
          <Field label="Payment method">
            <Select
              aria-label="Payment method"
              className="w-full"
              value={paymentForm.method}
              onChange={(event) => setPaymentForm({ ...paymentForm, method: event.target.value })}
            >
              <option value="check">Check</option>
              <option value="cash">Cash</option>
              <option value="card">District payment system</option>
              <option value="other">Other</option>
            </Select>
          </Field>
          <Field label="Date received">
            <Input
              required
              aria-label="Date received"
              type="date"
              value={paymentForm.received_on}
              onChange={(event) => setPaymentForm({ ...paymentForm, received_on: event.target.value })}
            />
          </Field>
          <Field label="Receipt reference">
            <Input
              required
              aria-label="Receipt reference"
              value={paymentForm.reference}
              onChange={(event) => setPaymentForm({ ...paymentForm, reference: event.target.value })}
            />
          </Field>
          {recordPayment.isError && <ErrorNote error={recordPayment.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setRecordingPayment(null)}>Cancel</Button>
            <Button type="submit" disabled={recordPayment.isPending}>
              {recordPayment.isPending ? 'Recording…' : 'Record payment'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && generatingLog !== null}
        title="Generate exemption log"
        onClose={() => setGeneratingLog(null)}
      >
        <div className="space-y-4">
          <p className="text-sm text-ink">
            Generate the RCW 42.56.210(3) exemption and withholding log for{' '}
            {generatingLog?.request_number}, installment {generatingLog?.sequence}?
          </p>
          <p className="text-sm text-ink">
            The PDF is derived from the approved review, citation, redaction, and
            second-review evidence. It is stored as an immutable artifact and included
            in the requester package.
          </p>
          {generateLog.isError && <ErrorNote error={generateLog.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setGeneratingLog(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={generateLog.isPending}
              onClick={() => generatingLog && generateLog.mutate(generatingLog.id)}
            >
              {generateLog.isPending ? 'Generating…' : 'Generate exemption log'}
            </Button>
          </div>
        </div>
      </Modal>
      <Modal
        open={canPrepare && preparing !== null}
        title="Prepare installment package"
        onClose={() => setPreparing(null)}
      >
        <div className="space-y-4">
          <p className="text-sm text-ink">
            Prepare {preparing?.request_number}, installment {preparing?.sequence}?
            The system will verify every source against its SHA-256 digest and recorded byte count.
          </p>
          <p className="text-sm text-ink">
            The resulting requester ZIP is an immutable legal artifact and cannot be overwritten.
            If the installment contents later change, prepare a new package.
          </p>
          {prepare.isError && <ErrorNote error={prepare.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setPreparing(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={prepare.isPending}
              onClick={() => preparing && prepare.mutate(preparing.id)}
            >
              {prepare.isPending ? 'Preparing…' : 'Prepare package'}
            </Button>
          </div>
        </div>
      </Modal>
      <Modal
        open={canPrepare && releasing !== null}
        title="Release installment"
        onClose={() => {
          setReleasing(null)
          setReleasedLink('')
          setReleasedNoticeId('')
        }}
      >
        {releasedLink ? (
          <div className="space-y-4">
            <p className="text-sm text-ink">
              The package is released and the requester notice has been sent
              under your name. The scoped URL is shown only in this response and
              the protected message; it is not exposed in the installment list.
            </p>
            <p className="text-xs text-ink-muted">
              Draft reference: {releasedNoticeId}
            </p>
            <Input
              aria-label="Requester download URL"
              readOnly
              value={releasedLink}
            />
            <div className="flex justify-end">
              <Button type="button" onClick={() => {
                setReleasing(null)
                setReleasedLink('')
                setReleasedNoticeId('')
              }}>
                Done
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <p className="text-sm text-ink">
              Release {releasing?.request_number}, installment {releasing?.sequence}
              {' '}and send the notice? This activates requester access to the
              verified package and delivers the installment notice, drafted by
              the server from this installment, under your name. The scoped link
              will expire.
            </p>
            <p className="text-sm text-ink">
              Confirm that any approved fee is paid and that the package is ready to communicate to the requester.
            </p>
            {releasing?.requires_access_code && (
              <p className="text-sm text-ink">
                This installment has an access code. Send the code to the
                requester in a separate message; it is not in this notice.
              </p>
            )}
            <label className="flex items-start gap-2 text-sm text-ink">
              <input
                type="checkbox"
                checked={isFinal}
                onChange={(event) => setIsFinal(event.target.checked)}
              />
              <span>
                This is the final installment. The generated notice will say so, but
                delivery will not automatically close the request.
              </span>
            </label>
            {release.isError && <ErrorNote error={release.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setReleasing(null)}>
                Cancel
              </Button>
              <Button
                type="button"
                disabled={release.isPending}
                onClick={() => releasing && release.mutate(releasing.id)}
              >
                {release.isPending ? 'Releasing…' : 'Release and send'}
              </Button>
            </div>
          </div>
        )}
      </Modal>
      <Modal
        open={canPrepare && editingContents !== null}
        title="Edit installment contents"
        onClose={() => setEditingContents(null)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (editingContents) setItems.mutate(editingContents.id)
          }}
        >
          <p className="text-sm text-ink">
            Contents can change only until a package is prepared. After that the
            installment is an immutable legal artifact; to ship a different set
            of records, create a new installment.
          </p>
          <div className="max-h-72 space-y-1 overflow-y-auto">
            {editCandidates.map((row) => (
              <label key={row.id} className="flex items-center gap-2 text-sm text-ink">
                <input
                  type="checkbox"
                  aria-label={`Include ${row.original_filename || row.id}`}
                  checked={editedItems.includes(row.id)}
                  onChange={() =>
                    setEditedItems((current) => (
                      current.includes(row.id)
                        ? current.filter((value) => value !== row.id)
                        : [...current, row.id]
                    ))
                  }
                />
                <span>{row.original_filename || row.id}</span>
              </label>
            ))}
          </div>
          {setItems.isError && <ErrorNote error={setItems.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setEditingContents(null)}>
              Cancel
            </Button>
            <Button type="submit" disabled={setItems.isPending || editedItems.length === 0}>
              {setItems.isPending ? 'Saving…' : `Save ${editedItems.length} records`}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && revoking !== null}
        title="Revoke requester download"
        onClose={() => setRevoking(null)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (revoking) revoke.mutate(revoking.id)
          }}
        >
          <p className="text-sm text-ink">
            Revoking ends the requester's access to installment{' '}
            {revoking?.sequence} of {revoking?.request_number}. The released
            package, its manifest, and the audit trail are untouched; only the
            link stops working. The requester sees a page telling them to
            contact the records officer.
          </p>
          <p className="text-sm text-ink">
            Its records become available to put into a new installment.
          </p>
          <Field
            label="Reason"
            hint="Recorded in the audit log. At least 10 characters."
          >
            <Textarea
              required
              minLength={MIN_REASON_LENGTH}
              aria-label="Reason for revoking"
              value={revokeReason}
              onChange={(event) => setRevokeReason(event.target.value)}
            />
          </Field>
          {revoke.isError && <ErrorNote error={revoke.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setRevoking(null)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={revoke.isPending || reasonTooShort(revokeReason)}
            >
              {revoke.isPending ? 'Revoking…' : 'Revoke download'}
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={canPrepare && codingAccess !== null}
        title="Installment access code"
        onClose={closeAccessCode}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (codingAccess) setCode.mutate(codingAccess.id)
          }}
        >
          <p className="text-sm text-ink">
            An access code adds a second factor to the download page. Send it to
            the requester in a separate message — never in the notice that
            carries the link. The code is stored only as a hash: nobody,
            including you, can read it back afterwards.
          </p>
          <Field label="Access code" hint="At least 8 characters, and not a single repeated or sequential run.">
            <Input
              required
              minLength={8}
              aria-label="Access code"
              value={accessCode}
              onChange={(event) => setAccessCode(event.target.value)}
            />
          </Field>
          {setCode.isError && <ErrorNote error={setCode.error} />}
          {clearCode.isError && <ErrorNote error={clearCode.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeAccessCode}>
              Cancel
            </Button>
            {codingAccess?.requires_access_code && (
              <Button
                type="button"
                variant="secondary"
                disabled={clearCode.isPending}
                onClick={() => codingAccess && clearCode.mutate(codingAccess.id)}
              >
                {clearCode.isPending ? 'Removing…' : 'Remove code'}
              </Button>
            )}
            <Button type="submit" disabled={setCode.isPending || accessCode.length < 8}>
              {setCode.isPending ? 'Saving…' : 'Set access code'}
            </Button>
          </div>
        </form>
      </Modal>
    </>
  )
}
