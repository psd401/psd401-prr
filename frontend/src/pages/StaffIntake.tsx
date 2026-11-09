// ABOUTME: Least-privilege staff entry for phone, mail, and in-person PRA
// ABOUTME: requests through the same canonical compliance intake pipeline.
import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import {
  Button,
  Card,
  ErrorNote,
  Field,
  Input,
  Select,
  Textarea,
} from '../ui'

interface IntakeValues {
  channel: 'phone' | 'mail' | 'in_person'
  /**
   * Which law governs the matter. Fixed at intake and never editable
   * afterwards, because it decides which clock, which letters, and which
   * report the matter is subject to (ADR 0004).
   */
  matter_kind: 'public_records' | 'litigation_discovery' | 'ferpa_parent'
  received_at: string
  description: string
  name: string
  email: string
  phone: string
  street_address: string
  city: string
  state: string
  postal_code: string
  company: string
}

interface IntakeResponse {
  number: string
}

const MAX_ATTACHMENT_COUNT = 10
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
const MAX_ATTACHMENT_TOTAL_BYTES = 50 * 1024 * 1024

function localDateTimeNow(): string {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

function initialValues(): IntakeValues {
  return {
    channel: 'phone',
    matter_kind: 'public_records',
    received_at: localDateTimeNow(),
    description: '',
    name: '',
    email: '',
    phone: '',
    street_address: '',
    city: '',
    state: '',
    postal_code: '',
    company: '',
  }
}

function attachmentError(files: File[]): string | undefined {
  if (files.length > MAX_ATTACHMENT_COUNT) return 'Attach no more than 10 files.'
  if (files.some((file) => file.size > MAX_ATTACHMENT_BYTES)) {
    return 'Each attachment must be 25 MB or smaller.'
  }
  if (files.reduce((total, file) => total + file.size, 0) > MAX_ATTACHMENT_TOTAL_BYTES) {
    return 'The selected attachments must total 50 MB or less.'
  }
  return undefined
}

function newIdempotencyKey(): string {
  return globalThis.crypto.randomUUID()
}

export function StaffIntake() {
  const [values, setValues] = useState<IntakeValues>(initialValues)
  const [attachments, setAttachments] = useState<File[]>([])
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey)
  const [error, setError] = useState<unknown>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [createdNumber, setCreatedNumber] = useState('')
  const [processingFailureNumber, setProcessingFailureNumber] = useState('')

  const setField = <K extends keyof IntakeValues>(field: K, value: IntakeValues[K]) => {
    setValues((current) => ({ ...current, [field]: value }))
    setError(null)
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (![values.email, values.phone, values.street_address].some((value) => value.trim())) {
      setError(new Error('Provide at least one response method: email, phone, or mailing address.'))
      return
    }
    const fileError = attachmentError(attachments)
    if (fileError) {
      setError(new Error(fileError))
      return
    }

    setError(null)
    setIsSubmitting(true)
    const body = new FormData()
    body.append('idempotency_key', idempotencyKey)
    body.append('channel', values.channel)
    body.append('matter_kind', values.matter_kind)
    body.append('received_at', new Date(values.received_at).toISOString())
    for (const field of [
      'description',
      'name',
      'email',
      'phone',
      'street_address',
      'city',
      'state',
      'postal_code',
      'company',
    ] as const) {
      body.append(field, values[field])
    }
    for (const file of attachments) body.append('attachments', file)

    try {
      const result = await api<IntakeResponse>('/api/staff/requests/intake/', {
        method: 'POST',
        body,
      })
      setCreatedNumber(result.number)
    } catch (submissionError) {
      if (submissionError instanceof ApiError && submissionError.data && typeof submissionError.data === 'object') {
        const number = (submissionError.data as Record<string, unknown>).number
        if (typeof number === 'string') {
          setProcessingFailureNumber(number)
          return
        }
      }
      setError(submissionError)
    } finally {
      setIsSubmitting(false)
    }
  }

  const reset = () => {
    setValues(initialValues())
    setAttachments([])
    setIdempotencyKey(newIdempotencyKey())
    setError(null)
    setCreatedNumber('')
    setProcessingFailureNumber('')
  }

  if (processingFailureNumber) {
    return (
      <>
        <PageHeader title="Staff intake" />
        <Card>
          <div role="alert" className="space-y-4 text-ink">
            <p className="font-heading text-sm font-semibold uppercase tracking-wide text-danger-ink">
              Request retained; follow-up required
            </p>
            <p className="font-mono text-3xl font-bold">{processingFailureNumber}</p>
            <p>
              The request was saved, but downstream processing did not finish. Do not submit it again.
              Give this request number to a Records Officer for recovery.
            </p>
          </div>
        </Card>
      </>
    )
  }

  if (createdNumber) {
    return (
      <>
        <PageHeader title="Staff intake" />
        <Card>
          <div role="status" className="space-y-4 text-ink">
            <p className="font-heading text-sm font-semibold uppercase tracking-wide text-success-ink">
              Request logged
            </p>
            <p className="font-mono text-3xl font-bold">{createdNumber}</p>
            <p>The request has entered the compliance queue for deadline calculation and triage.</p>
            <Button type="button" onClick={reset}>Log another request</Button>
          </div>
        </Card>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Staff intake"
        subtitle="Log a phone, mailed, or in-person request using the time it actually arrived."
      />
      <form onSubmit={submit} className="space-y-5">
        <ErrorNote error={error} />
        <Card title="Receipt">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="How was the request received?">
              <Select
                required
                className="w-full"
                value={values.channel}
                onChange={(event) => setField('channel', event.target.value as IntakeValues['channel'])}
              >
                <option value="phone">Phone</option>
                <option value="mail">US mail or paper delivery</option>
                <option value="in_person">In person</option>
              </Select>
            </Field>
            <Field
              label="What kind of matter is this?"
              hint="Fixed once the matter is opened. It decides the deadline, the letters allowed, and whether the matter is reported to JLARC."
            >
              <Select
                required
                aria-label="What kind of matter is this?"
                className="w-full"
                value={values.matter_kind}
                onChange={(event) =>
                  setField(
                    'matter_kind',
                    event.target.value as IntakeValues['matter_kind'],
                  )
                }
              >
                <option value="public_records">
                  Public records request (RCW 42.56)
                </option>
                <option value="litigation_discovery">
                  Litigation / discovery pull
                </option>
                <option value="ferpa_parent">
                  FERPA parent or eligible student (34 CFR 99.10)
                </option>
              </Select>
            </Field>
            <Field
              label="Date and time received"
              hint="Use the original arrival time, not the time this form is entered."
            >
              <Input
                required
                aria-label="Date and time received"
                type="datetime-local"
                value={values.received_at}
                onChange={(event) => setField('received_at', event.target.value)}
              />
            </Field>
          </div>
          <div className="mt-4">
            <Field label="What records were requested?" hint="Preserve the requester's wording.">
              <Textarea
                required
                aria-label="What records were requested?"
                rows={8}
                maxLength={100_000}
                value={values.description}
                onChange={(event) => setField('description', event.target.value)}
              />
            </Field>
          </div>
        </Card>

        <Card title="Response information">
          <p className="mb-4 text-sm text-ink-muted">
            Provide at least one response method. Do not ask for identity or purpose beyond what is needed to respond.
          </p>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Full name (optional)">
              <Input value={values.name} onChange={(event) => setField('name', event.target.value)} />
            </Field>
            <Field label="Company or organization (optional)">
              <Input value={values.company} onChange={(event) => setField('company', event.target.value)} />
            </Field>
            <Field label="Email address (optional)">
              <Input type="email" value={values.email} onChange={(event) => setField('email', event.target.value)} />
            </Field>
            <Field label="Phone number (optional)">
              <Input type="tel" value={values.phone} onChange={(event) => setField('phone', event.target.value)} />
            </Field>
            <div className="md:col-span-2">
              <Field label="Mailing address (optional)">
                <Input value={values.street_address} onChange={(event) => setField('street_address', event.target.value)} />
              </Field>
            </div>
            <Field label="City (optional)">
              <Input value={values.city} onChange={(event) => setField('city', event.target.value)} />
            </Field>
            <Field label="State (optional)">
              <Input value={values.state} onChange={(event) => setField('state', event.target.value)} />
            </Field>
            <Field label="Postal code (optional)">
              <Input value={values.postal_code} onChange={(event) => setField('postal_code', event.target.value)} />
            </Field>
          </div>
        </Card>

        <Card title="Files">
          <Field
            label="Scanned request or supporting files (optional)"
            hint="Up to 10 files, 25 MB each and 50 MB total. Files remain quarantined until clean."
          >
            <Input
              aria-label="Scanned request or supporting files (optional)"
              type="file"
              multiple
              onChange={(event) => {
                setAttachments(Array.from(event.target.files ?? []))
                setError(null)
              }}
            />
          </Field>
        </Card>

        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? 'Logging request…' : 'Log request'}
        </Button>
      </form>
    </>
  )
}
