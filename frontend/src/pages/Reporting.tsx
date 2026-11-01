// ABOUTME: Human-reviewed JLARC annual worksheet, inputs, and data-quality gaps.
// ABOUTME: The UI exports a transfer aid and never submits data to JLARC.
import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { StaffRole } from '../api/auth'
import { api } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import {
  Badge,
  Button,
  Card,
  ErrorNote,
  Field,
  Input,
  Modal,
  Select,
  Spinner,
  Table,
  Textarea,
} from '../ui'

interface RequestLogRow {
  request_number: string
  requester_identity: string
  received_date: string
  original_request: string
  records_produced_description: string
  records_withheld_description: string
  withholding_reasons: string[]
  final_disposition_date: string | null
}

interface JlarcMetric {
  label: string
  [key: string]: unknown
}

interface JlarcReport {
  reporting_year: number
  period: { start: string; end_exclusive: string }
  guidance: {
    statute: string
    metric_guidance_version: string
    submission: string
  }
  supplement: {
    revision: number
    methodology_notes: string
    created_by: string
  }
  baseline: Record<string, number>
  metrics: Record<string, JlarcMetric>
  quality_warnings: string[]
  request_log: RequestLogRow[]
  data_sha256: string
  generated_at: string
}

const moneyFields = [
  ['Staff compensation cost', 'staff_compensation_cost'],
  ['Non-litigation legal review cost', 'legal_review_cost'],
  ['Supplies and services cost', 'supplies_services_cost'],
  ['Agency litigation cost', 'litigation_agency_cost'],
  ['Settlements', 'litigation_settlements'],
  ['Penalties', 'litigation_penalties'],
  ["Requester attorney fees", 'litigation_requester_attorney_fees'],
  ['Requester litigation costs', 'litigation_requester_costs'],
  ['Records-management staff cost', 'records_management_staff_cost'],
  ['Records systems and services cost', 'records_management_systems_cost'],
  ['Other records-management cost', 'records_management_other_cost'],
  ['Other expenses recovered', 'other_expenses_recovered'],
  ['External customized-service recovery', 'customized_service_expenses_recovered_external'],
] as const

function emptySupplement() {
  return {
    unallocated_staff_minutes: '0',
    staff_compensation_cost: '',
    legal_review_cost: '',
    supplies_services_cost: '',
    claims_filed: '0',
    claims_by_type: '{}',
    claims_by_exemption: '{}',
    litigation_agency_cost: '',
    litigation_settlements: '',
    litigation_penalties: '',
    litigation_requester_attorney_fees: '',
    litigation_requester_costs: '',
    records_management_staff_cost: '',
    records_management_systems_cost: '',
    records_management_other_cost: '',
    other_expenses_recovered: '',
    customized_service_expenses_recovered_external: '',
    methodology_notes: '',
  }
}

function emptyClassification() {
  return {
    requester_type: 'unknown',
    fulfillment_format: 'electronic',
    scanned_physical: false,
    records_produced_description: '',
    records_withheld_description: '',
    reason: '',
  }
}

/**
 * Claim breakdowns are a small map of label -> count. Asking a records officer
 * to hand-type JSON into a textarea -- and only telling them it was malformed
 * after they submitted the whole worksheet -- put a syntax error between them
 * and a statutory filing. The JSON string stays the wire format; this just
 * stops anyone having to write it.
 */
function ClaimBreakdown({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string
  value: string
  onChange: (next: string) => void
  disabled: boolean
}) {
  let parsed: Record<string, unknown> = {}
  try {
    const candidate = JSON.parse(value || '{}')
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
      parsed = candidate as Record<string, unknown>
    }
  } catch {
    parsed = {}
  }
  const rows = Object.entries(parsed)

  function write(next: Array<[string, unknown]>) {
    onChange(JSON.stringify(Object.fromEntries(next.filter(([key]) => key.trim()))))
  }

  return (
    <fieldset className="space-y-2">
      <legend className="font-heading text-sm font-semibold text-pacific">{label}</legend>
      {rows.length === 0 && (
        <p className="text-sm text-pacific/80">None recorded.</p>
      )}
      {rows.map(([key, count], index) => (
        <div key={index} className="flex flex-wrap items-end gap-2">
          <div className="min-w-48 flex-1">
            <Field label="Category">
              <Input
                value={key}
                disabled={disabled}
                onChange={(event) => {
                  const next = [...rows] as Array<[string, unknown]>
                  next[index] = [event.target.value, count]
                  write(next)
                }}
              />
            </Field>
          </div>
          <div className="w-28">
            <Field label="Claims">
              <Input
                type="number"
                min={0}
                value={String(count ?? 0)}
                disabled={disabled}
                onChange={(event) => {
                  const next = [...rows] as Array<[string, unknown]>
                  next[index] = [key, Number(event.target.value || 0)]
                  write(next)
                }}
              />
            </Field>
          </div>
          <Button
            type="button"
            variant="ghost"
            disabled={disabled}
            aria-label={`Remove ${key || 'category'}`}
            onClick={() => write(rows.filter((_, i) => i !== index) as Array<[string, unknown]>)}
          >
            Remove
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="ghost"
        disabled={disabled}
        onClick={() => write([...rows, ['', 0]] as Array<[string, unknown]>)}
      >
        Add category
      </Button>
    </fieldset>
  )
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export function Reporting({ roles }: { roles: StaffRole[] }) {
  const qc = useQueryClient()
  const canWrite = roles.includes('records_officer')
  const [year, setYear] = useState(new Date().getFullYear() - 1)
  const [recordingInputs, setRecordingInputs] = useState(false)
  const [supplement, setSupplement] = useState(emptySupplement)
  const [classifying, setClassifying] = useState<RequestLogRow | null>(null)
  const [classification, setClassification] = useState(emptyClassification)
  const [formError, setFormError] = useState('')

  const report = useQuery({
    queryKey: ['jlarc-report', year],
    queryFn: () => api<JlarcReport>(`/api/staff/jlarc-reports/${year}/`),
    retry: false,
  })
  const saveSupplement = useMutation({
    mutationFn: (body: Record<string, unknown>) => api(
      `/api/staff/jlarc-reports/${year}/supplement/`,
      { method: 'POST', body },
    ),
    onSuccess: async () => {
      setRecordingInputs(false)
      await qc.invalidateQueries({ queryKey: ['jlarc-report', year] })
    },
  })
  const saveClassification = useMutation({
    mutationFn: ({ row, body }: { row: RequestLogRow; body: typeof classification }) => api(
      `/api/staff/requests/${row.request_number}/jlarc-classification/`,
      { method: 'POST', body },
    ),
    onSuccess: async () => {
      setClassifying(null)
      await qc.invalidateQueries({ queryKey: ['jlarc-report', year] })
    },
  })

  function submitSupplement(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      const body: Record<string, unknown> = {
        ...supplement,
        unallocated_staff_minutes: Number(supplement.unallocated_staff_minutes || 0),
        claims_filed: Number(supplement.claims_filed || 0),
        claims_by_type: JSON.parse(supplement.claims_by_type || '{}'),
        claims_by_exemption: JSON.parse(supplement.claims_by_exemption || '{}'),
      }
      for (const [, key] of moneyFields) body[key] = supplement[key] || '0.00'
      saveSupplement.mutate(body)
    } catch {
      setFormError('Claim breakdowns must be valid JSON objects, for example {"PRA timeliness": 2}.')
    }
  }

  return (
    <>
      <PageHeader
        title="JLARC reporting"
        subtitle="Prepare the annual RCW 40.14.026 worksheet for accountable human review and transfer."
        actions={(
          <div className="flex items-end gap-2">
            <Field label="Reporting year">
              <Input
                aria-label="Reporting year"
                type="number"
                min="2017"
                max={new Date().getFullYear()}
                value={year}
                onChange={(event) => setYear(Number(event.target.value))}
              />
            </Field>
            {canWrite && (
              <Button type="button" onClick={() => {
                saveSupplement.reset()
                setSupplement(emptySupplement())
                setRecordingInputs(true)
              }}>
                Record annual inputs
              </Button>
            )}
          </div>
        )}
      />

      {report.isPending ? <Spinner /> : report.isError ? (
        <Card title="Annual inputs required">
          <div className="space-y-3">
            <ErrorNote error={report.error} />
            <p className="text-sm text-pacific/80">
              Finance, HR, risk-management, litigation, retention, and unallocated-time totals cannot be inferred safely. A Records Officer must record their reconciled source and methodology before the worksheet is generated.
            </p>
          </div>
        </Card>
      ) : report.data ? (
        <div className="space-y-5">
          <Card
            title={`${year} JLARC worksheet`}
            actions={(
              <a
                className="inline-flex rounded-lg bg-cedar px-3.5 py-2 font-heading text-sm font-semibold text-white hover:bg-pacific"
                href={`/api/staff/jlarc-reports/${year}/export-csv/`}
              >
                Download transfer worksheet
              </a>
            )}
          >
            <div className="space-y-3 text-sm text-pacific">
              <p>{report.data.guidance.submission}</p>
              <p>
                <Badge tone="blue">Supplement revision {report.data.supplement.revision}</Badge>{' '}
                {report.data.supplement.methodology_notes}
              </p>
              <p className="font-mono text-xs break-all">{report.data.data_sha256}</p>
            </div>
          </Card>

          {report.data.quality_warnings.length > 0 && (
            <Card title="Data quality review">
              <ul className="list-disc space-y-2 pl-5 text-sm text-amber-900">
                {report.data.quality_warnings.map((warning) => <li key={warning}>{warning}</li>)}
              </ul>
            </Card>
          )}

          <Card title="Baseline">
            <dl className="grid gap-3 md:grid-cols-5">
              {Object.entries(report.data.baseline).map(([field, value]) => (
                <div key={field} className="rounded-lg bg-skylight p-3">
                  <dt className="text-xs text-pacific/80">{field.replaceAll('_', ' ')}</dt>
                  <dd className="font-mono text-2xl font-bold text-pacific">{value}</dd>
                </div>
              ))}
            </dl>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            {Object.entries(report.data.metrics).map(([metricId, metric]) => (
              <Card key={metricId} title={metric.label}>
                <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-pacific/80">Metric {metricId}</p>
                <dl className="space-y-2 text-sm">
                  {Object.entries(metric).filter(([field]) => field !== 'label').map(([field, value]) => (
                    <div key={field} className="flex justify-between gap-4 border-b border-driftwood/30 pb-1">
                      <dt className="text-pacific/80">{field.replaceAll('_', ' ')}</dt>
                      <dd className="max-w-[60%] text-right font-mono text-pacific">{displayValue(value)}</dd>
                    </div>
                  ))}
                </dl>
              </Card>
            ))}
          </div>

          <Card title="RCW 40.14.026(4) request log">
            <Table
              columns={[
                { header: 'Request', cell: (row: RequestLogRow) => row.request_number },
                { header: 'Received', cell: (row: RequestLogRow) => row.received_date },
                { header: 'Final disposition', cell: (row: RequestLogRow) => row.final_disposition_date ?? 'Open' },
                { header: 'Produced', cell: (row: RequestLogRow) => row.records_produced_description || 'Needs classification' },
                { header: 'Withheld', cell: (row: RequestLogRow) => row.records_withheld_description || 'Needs classification' },
                ...(canWrite ? [{
                  header: 'Action',
                  cell: (row: RequestLogRow) => (
                    <Button type="button" variant="ghost" onClick={() => {
                      saveClassification.reset()
                      setClassification(emptyClassification())
                      setClassifying(row)
                    }}>
                      Classify
                    </Button>
                  ),
                }] : []),
              ]}
              rows={report.data.request_log}
              rowKey={(row) => row.request_number}
            />
          </Card>
        </div>
      ) : null}

      <Modal
        open={canWrite && recordingInputs}
        title="Record JLARC annual inputs"
        size="wide"
        onClose={() => setRecordingInputs(false)}
      >
        <form className="space-y-5" onSubmit={submitSupplement}>
          <p className="text-sm text-pacific">
            Record reconciled totals for {year}. This creates an immutable revision; it does not submit anything to JLARC.
          </p>
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Unallocated staff minutes">
              <Input
                type="number"
                min="0"
                value={supplement.unallocated_staff_minutes}
                onChange={(event) => setSupplement({ ...supplement, unallocated_staff_minutes: event.target.value })}
              />
            </Field>
            <Field label="Claims filed">
              <Input
                type="number"
                min="0"
                value={supplement.claims_filed}
                onChange={(event) => setSupplement({ ...supplement, claims_filed: event.target.value })}
              />
            </Field>
            {moneyFields.map(([label, key]) => (
              <Field key={key} label={label}>
                <Input
                  aria-label={label}
                  type="number"
                  min="0"
                  step="0.01"
                  value={supplement[key]}
                  onChange={(event) => setSupplement({ ...supplement, [key]: event.target.value })}
                />
              </Field>
            ))}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <ClaimBreakdown
              label="Claims by type"
              value={supplement.claims_by_type}
              disabled={!canWrite}
              onChange={(next) => setSupplement({ ...supplement, claims_by_type: next })}
            />
            <ClaimBreakdown
              label="Claims by exemption"
              value={supplement.claims_by_exemption}
              disabled={!canWrite}
              onChange={(next) => setSupplement({ ...supplement, claims_by_exemption: next })}
            />
          </div>
          <Field label="Methodology notes">
            <Textarea
              required
              aria-label="Methodology notes"
              rows={4}
              value={supplement.methodology_notes}
              onChange={(event) => setSupplement({ ...supplement, methodology_notes: event.target.value })}
            />
          </Field>
          {formError && <p role="alert" className="text-sm text-red-800">{formError}</p>}
          {saveSupplement.isError && <ErrorNote error={saveSupplement.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setRecordingInputs(false)}>Cancel</Button>
            <Button type="submit" disabled={saveSupplement.isPending}>
              {saveSupplement.isPending ? 'Saving…' : 'Save annual inputs'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal
        open={canWrite && classifying !== null}
        title="Classify request for JLARC"
        onClose={() => setClassifying(null)}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (classifying) saveClassification.mutate({ row: classifying, body: classification })
          }}
        >
          <p className="text-sm text-pacific">
            Use only information already volunteered or established by the file. Do not contact or prompt the requester for identity or requester type.
          </p>
          <Field label="Requester type">
            <Select
              className="w-full"
              value={classification.requester_type}
              onChange={(event) => setClassification({ ...classification, requester_type: event.target.value })}
            >
              <option value="unknown">Unknown / anonymous</option>
              <option value="individual">Individual</option>
              <option value="law_firm">Law firm</option>
              <option value="organization">Organization / business</option>
              <option value="insurer">Insurer</option>
              <option value="government">Government</option>
              <option value="incarcerated">Incarcerated person</option>
              <option value="media">Media</option>
              <option value="employee">Current or former employee</option>
              <option value="other">Other</option>
            </Select>
          </Field>
          <Field label="Fulfillment format">
            <Select
              className="w-full"
              value={classification.fulfillment_format}
              onChange={(event) => setClassification({ ...classification, fulfillment_format: event.target.value })}
            >
              <option value="electronic">Entirely electronic</option>
              <option value="physical">Entirely physical</option>
              <option value="both">Electronic and physical</option>
              <option value="no_records_or_other">No records or otherwise closed without fulfillment</option>
            </Select>
          </Field>
          <label className="flex gap-2 text-sm text-pacific">
            <input
              type="checkbox"
              checked={classification.scanned_physical}
              onChange={(event) => setClassification({ ...classification, scanned_physical: event.target.checked })}
            />
            One or more physical records were scanned to fulfill disclosure.
          </label>
          <Field label="Records produced description">
            <Textarea
              required
              aria-label="Records produced description"
              rows={3}
              value={classification.records_produced_description}
              onChange={(event) => setClassification({ ...classification, records_produced_description: event.target.value })}
            />
          </Field>
          <Field label="Records withheld description">
            <Textarea
              required
              aria-label="Records withheld description"
              rows={3}
              value={classification.records_withheld_description}
              onChange={(event) => setClassification({ ...classification, records_withheld_description: event.target.value })}
            />
          </Field>
          <Field label="Classification reason">
            <Textarea
              required
              aria-label="Classification reason"
              rows={3}
              value={classification.reason}
              onChange={(event) => setClassification({ ...classification, reason: event.target.value })}
            />
          </Field>
          {saveClassification.isError && <ErrorNote error={saveClassification.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setClassifying(null)}>Cancel</Button>
            <Button type="submit" disabled={saveClassification.isPending}>
              {saveClassification.isPending ? 'Saving…' : 'Save classification'}
            </Button>
          </div>
        </form>
      </Modal>
    </>
  )
}
