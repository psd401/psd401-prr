// ABOUTME: Accessible anonymous Public Records Act request form.
// ABOUTME: It preserves requester input on failure and submits through C1.
import { useState, type FormEvent, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import {
  isSupportedPublicAttachment,
  PUBLIC_ATTACHMENT_ACCEPT,
  PublicIntakeError,
  submitPublicIntake,
  type PublicIntakeValues,
} from '../api/publicIntake'
import { Button, Input, Textarea } from '../ui'
import { PublicRequestLayout } from './PublicRequestLayout'

const MAX_ATTACHMENT_COUNT = 10
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
const MAX_ATTACHMENT_TOTAL_BYTES = 50 * 1024 * 1024
const PUBLIC_CONTROL_CLASS = 'border-ocean text-base focus:border-whulge focus:outline-whulge'

const INITIAL_VALUES: PublicIntakeValues = {
  description: '',
  email: '',
  name: '',
  phone: '',
  street_address: '',
  city: '',
  state: '',
  postal_code: '',
  company: '',
  website: '',
}

type VisibleField = Exclude<keyof PublicIntakeValues, 'website'>
type ErrorKey = VisibleField | 'attachments' | 'form'
type FormErrors = Partial<Record<ErrorKey, string>>

function FieldBlock({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string
  label: string
  hint?: string
  error?: string
  children: ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block font-heading text-sm font-semibold text-pacific">
        {label}
      </label>
      {children}
      {hint && <p id={`${id}-hint`} className="text-sm text-pacific/80">{hint}</p>}
      {error && (
        <p id={`${id}-error`} role="alert" className="text-sm font-semibold text-red-800">
          {error}
        </p>
      )}
    </div>
  )
}

function describedBy(id: string, hint: boolean, error?: string): string | undefined {
  const ids = [hint ? `${id}-hint` : '', error ? `${id}-error` : ''].filter(Boolean)
  return ids.length ? ids.join(' ') : undefined
}

function firstError(value: unknown): string | undefined {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    const messages = value.filter((item): item is string => typeof item === 'string')
    return messages.length ? messages.join(' ') : undefined
  }
  return undefined
}

function serverFieldErrors(data: unknown): FormErrors {
  if (!data || typeof data !== 'object') return {}
  const payload = data as Record<string, unknown>
  const fields: ErrorKey[] = [
    'description', 'email', 'name', 'phone', 'street_address', 'city',
    'state', 'postal_code', 'company', 'attachments',
  ]
  return Object.fromEntries(
    fields.flatMap((field) => {
      const message = firstError(payload[field])
      return message ? [[field, message]] : []
    }),
  ) as FormErrors
}

function validateAttachments(files: File[]): string | undefined {
  if (files.length > MAX_ATTACHMENT_COUNT) {
    return `A maximum of ${MAX_ATTACHMENT_COUNT} files may be attached.`
  }
  let totalBytes = 0
  for (const file of files) {
    if (file.size > MAX_ATTACHMENT_BYTES) {
      return `${file.name} is larger than the 25 MB per-file limit.`
    }
    totalBytes += file.size
    if (!isSupportedPublicAttachment(file)) {
      return `${file.name} is not an accepted document or image type.`
    }
  }
  if (totalBytes > MAX_ATTACHMENT_TOTAL_BYTES) {
    return 'The selected files exceed the 50 MB combined limit.'
  }
  return undefined
}

export function PublicRequestForm() {
  const navigate = useNavigate()
  const [values, setValues] = useState<PublicIntakeValues>(INITIAL_VALUES)
  const [attachments, setAttachments] = useState<File[]>([])
  const [errors, setErrors] = useState<FormErrors>({})
  const [isSubmitting, setIsSubmitting] = useState(false)

  const setField = (field: keyof PublicIntakeValues, value: string) => {
    setValues((current) => ({ ...current, [field]: value }))
    setErrors((current) => ({ ...current, [field]: undefined, form: undefined }))
  }

  const onAttachmentChange = (files: File[]) => {
    const error = validateAttachments(files)
    setErrors((current) => ({ ...current, attachments: error, form: undefined }))
    setAttachments(files)
  }

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const attachmentError = errors.attachments ?? validateAttachments(attachments)
    if (attachmentError) {
      setErrors((current) => ({ ...current, attachments: attachmentError }))
      return
    }
    setErrors({})
    setIsSubmitting(true)
    try {
      const result = await submitPublicIntake(values, attachments)
      navigate(`/request/submitted?number=${encodeURIComponent(result.number)}`, {
        replace: true,
      })
    } catch (error) {
      if (error instanceof PublicIntakeError) {
        const fieldErrors = serverFieldErrors(error.data)
        setErrors({
          ...fieldErrors,
          ...(Object.keys(fieldErrors).length ? {} : { form: error.message }),
        })
      } else {
        setErrors({
          form: 'We could not connect to the district. Check your connection and try again.',
        })
      }
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <PublicRequestLayout>
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_18rem] lg:items-start">
        <section aria-labelledby="public-request-heading">
          <div className="mb-7 max-w-3xl">
            <p className="mb-2 font-heading text-sm font-semibold uppercase tracking-[0.08em] text-cedar">
              Washington Public Records Act
            </p>
            <h1 id="public-request-heading" className="text-3xl font-bold text-pacific sm:text-4xl">
              Request public records
            </h1>
            <p className="mt-3 max-w-2xl text-base text-pacific/80 sm:text-lg">
              Describe the records you are looking for as clearly as you can. You do not need to
              explain why you want them or verify your identity.
            </p>
          </div>

          <form
            onSubmit={onSubmit}
            className="space-y-7 rounded-2xl border border-driftwood/70 bg-white p-5 shadow-[0_1px_2px_rgba(37,66,76,0.06),0_12px_32px_rgba(37,66,76,0.06)] sm:p-8"
          >
            {errors.form && (
              <div role="alert" className="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-900">
                <p className="font-semibold">We could not submit your request.</p>
                <p className="mt-1">{errors.form}</p>
              </div>
            )}

            <FieldBlock
              id="description"
              label="What records are you requesting?"
              hint="Include useful details such as dates, departments, people, topics, or document types."
              error={errors.description}
            >
              <Textarea
                id="description"
                name="description"
                value={values.description}
                onChange={(event) => setField('description', event.target.value)}
                required
                maxLength={100_000}
                rows={9}
                aria-invalid={Boolean(errors.description)}
                aria-describedby={describedBy('description', true, errors.description)}
                className={`${PUBLIC_CONTROL_CLASS} min-h-44 resize-y`}
              />
            </FieldBlock>

            <div className="border-t border-driftwood/60 pt-7">
              <h2 className="text-xl font-bold">How should we contact you?</h2>
              <p className="mt-1 text-sm text-pacific/80">
                Email is required so the district can respond. Everything else is optional.
              </p>
              <div className="mt-5 grid gap-5 sm:grid-cols-2">
                <FieldBlock id="email" label="Email address" error={errors.email}>
                  <Input
                    id="email"
                    name="email"
                    type="email"
                    autoComplete="email"
                    value={values.email}
                    onChange={(event) => setField('email', event.target.value)}
                    required
                    aria-invalid={Boolean(errors.email)}
                    aria-describedby={describedBy('email', false, errors.email)}
                    className={PUBLIC_CONTROL_CLASS}
                  />
                </FieldBlock>
                <FieldBlock id="name" label="Full name (optional)" error={errors.name}>
                  <Input
                    id="name"
                    name="name"
                    autoComplete="name"
                    maxLength={255}
                    value={values.name}
                    onChange={(event) => setField('name', event.target.value)}
                    aria-invalid={Boolean(errors.name)}
                    aria-describedby={describedBy('name', false, errors.name)}
                    className={PUBLIC_CONTROL_CLASS}
                  />
                </FieldBlock>
                <FieldBlock id="phone" label="Phone number (optional)" error={errors.phone}>
                  <Input
                    id="phone"
                    name="phone"
                    type="tel"
                    autoComplete="tel"
                    maxLength={40}
                    value={values.phone}
                    onChange={(event) => setField('phone', event.target.value)}
                    aria-invalid={Boolean(errors.phone)}
                    aria-describedby={describedBy('phone', false, errors.phone)}
                    className={PUBLIC_CONTROL_CLASS}
                  />
                </FieldBlock>
                <FieldBlock id="company" label="Company or organization (optional)" error={errors.company}>
                  <Input
                    id="company"
                    name="company"
                    autoComplete="organization"
                    maxLength={255}
                    value={values.company}
                    onChange={(event) => setField('company', event.target.value)}
                    aria-invalid={Boolean(errors.company)}
                    aria-describedby={describedBy('company', false, errors.company)}
                    className={PUBLIC_CONTROL_CLASS}
                  />
                </FieldBlock>
                <div className="sm:col-span-2">
                  <FieldBlock id="street_address" label="Mailing address (optional)" error={errors.street_address}>
                    <Input
                      id="street_address"
                      name="street_address"
                      autoComplete="street-address"
                      maxLength={1000}
                      value={values.street_address}
                      onChange={(event) => setField('street_address', event.target.value)}
                      aria-invalid={Boolean(errors.street_address)}
                      aria-describedby={describedBy('street_address', false, errors.street_address)}
                      className={PUBLIC_CONTROL_CLASS}
                    />
                  </FieldBlock>
                </div>
                <FieldBlock id="city" label="City (optional)" error={errors.city}>
                  <Input
                    id="city"
                    name="city"
                    autoComplete="address-level2"
                    maxLength={255}
                    value={values.city}
                    onChange={(event) => setField('city', event.target.value)}
                    aria-invalid={Boolean(errors.city)}
                    aria-describedby={describedBy('city', false, errors.city)}
                    className={PUBLIC_CONTROL_CLASS}
                  />
                </FieldBlock>
                <FieldBlock id="state" label="State (optional)" error={errors.state}>
                  <Input
                    id="state"
                    name="state"
                    autoComplete="address-level1"
                    maxLength={100}
                    value={values.state}
                    onChange={(event) => setField('state', event.target.value)}
                    aria-invalid={Boolean(errors.state)}
                    aria-describedby={describedBy('state', false, errors.state)}
                    className={PUBLIC_CONTROL_CLASS}
                  />
                </FieldBlock>
                <FieldBlock id="postal_code" label="Postal code (optional)" error={errors.postal_code}>
                  <Input
                    id="postal_code"
                    name="postal_code"
                    autoComplete="postal-code"
                    maxLength={32}
                    value={values.postal_code}
                    onChange={(event) => setField('postal_code', event.target.value)}
                    aria-invalid={Boolean(errors.postal_code)}
                    aria-describedby={describedBy('postal_code', false, errors.postal_code)}
                    className={PUBLIC_CONTROL_CLASS}
                  />
                </FieldBlock>
              </div>
            </div>

            <div className="border-t border-driftwood/60 pt-7">
              <FieldBlock
                id="attachments"
                label="Attachments (optional)"
                hint="Accepted documents and images: up to 10 files, 25 MB each and 50 MB total."
                error={errors.attachments}
              >
                <Input
                  id="attachments"
                  name="attachments"
                  type="file"
                  accept={PUBLIC_ATTACHMENT_ACCEPT}
                  multiple
                  onChange={(event) => onAttachmentChange(Array.from(event.target.files ?? []))}
                  aria-invalid={Boolean(errors.attachments)}
                  aria-describedby={describedBy('attachments', true, errors.attachments)}
                  className={`${PUBLIC_CONTROL_CLASS} cursor-pointer file:mr-3 file:rounded-md file:border-0 file:bg-seafoam file:px-3 file:py-1.5 file:font-semibold file:text-pacific`}
                />
              </FieldBlock>
              {attachments.length > 0 && (
                <ul className="mt-3 space-y-1 text-sm text-pacific/80" aria-label="Selected attachments">
                  {attachments.map((file) => <li key={`${file.name}-${file.size}`}>{file.name}</li>)}
                </ul>
              )}
            </div>

            <div className="absolute -left-[10000px] top-auto size-px overflow-hidden" aria-hidden="true">
              <label htmlFor="website">Leave this field blank</label>
              <input
                id="website"
                name="website"
                tabIndex={-1}
                autoComplete="off"
                value={values.website}
                onChange={(event) => setField('website', event.target.value)}
              />
            </div>

            <div className="flex flex-col gap-3 border-t border-driftwood/60 pt-7 sm:flex-row sm:items-center sm:justify-between">
              <p className="max-w-md text-sm text-pacific/80">
                Your description becomes the legal record of what you requested. Review it before submitting.
              </p>
              <Button
                type="submit"
                variant="secondary"
                disabled={isSubmitting}
                className="min-h-11 justify-center px-6 text-base focus-visible:outline-whulge sm:shrink-0"
              >
                {isSubmitting ? 'Submitting…' : 'Submit request'}
              </Button>
            </div>
          </form>
        </section>

        <aside className="rounded-2xl border border-ocean/35 bg-skylight p-5 lg:sticky lg:top-6" aria-labelledby="what-happens-next">
          <h2 id="what-happens-next" className="text-lg font-bold">What happens next</h2>
          <p className="mt-2 text-sm text-pacific/80">
            The district will respond within five business days. The response may provide the records,
            give an estimated delivery date, ask for clarification, or explain a lawful exemption.
          </p>
          <p className="mt-4 text-sm text-pacific/80">
            Public records are existing district records. This process is not for asking general
            questions or requesting that a new document be created.
          </p>
        </aside>
      </div>
    </PublicRequestLayout>
  )
}
