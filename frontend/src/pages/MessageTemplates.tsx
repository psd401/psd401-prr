// ABOUTME: Governed configuration for every district-editable message template.
// ABOUTME: Active content is immutable; officers edit and explicitly activate new versions.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { ApiError, api } from '../api/client'
import type { StaffRole } from '../api/auth'
import { PageHeader } from '../layout/AppLayout'
import {
  Badge,
  Button,
  Card,
  ErrorNote,
  Field,
  Input,
  Modal,
  Spinner,
  Textarea,
  useToast,
  MIN_REASON_LENGTH,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
} from '../ui'

/** One rule the edited wording trips, in the officer's terms. */
interface GuardrailWarning {
  code: string
  message: string
  consequence: string
}

interface GuardrailPrompt {
  mayOverride: boolean
  detail: string
  warnings: GuardrailWarning[]
}

/**
 * Recognize the 409 the API sends when wording trips a guardrail.
 *
 * This is deliberately not treated as an error. The request was well formed
 * and the officer is entitled to publish it -- they just have not said why
 * yet. Everything else still goes to the toast.
 */
function guardrailPrompt(error: unknown): GuardrailPrompt | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const data = error.data as Record<string, unknown> | null
  if (!data || !Array.isArray(data.guardrail_warnings)) return null
  return {
    mayOverride: data.may_override === true,
    detail: typeof data.detail === 'string' ? data.detail : error.message,
    warnings: data.guardrail_warnings as GuardrailWarning[],
  }
}

interface MessageTemplateVersion {
  id: string | null
  kind: string
  template_version: number | string
  state: 'draft' | 'active' | 'retired'
  source: 'built_in' | 'managed'
  subject_template: string
  body_template: string
  change_reason: string
  guardrail_overrides?: string[]
  guardrail_override_reason?: string
  created_by_username: string | null
  activated_by_username: string | null
  created_at: string | null
  updated_at: string | null
  activated_at: string | null
  revision: number | null
}

interface MessageTemplateSlot {
  kind: string
  label: string
  group: string
  authoring: 'structured' | 'free_form'
  allowed_tokens: string[]
  required_tokens: string[]
  active: MessageTemplateVersion
  drafts: MessageTemplateVersion[]
  history: MessageTemplateVersion[]
}

interface Catalog {
  templates: MessageTemplateSlot[]
}

interface Preview {
  subject: string
  body: string
}

export function MessageTemplates({ roles }: { roles: StaffRole[] }) {
  const canEdit = roles.includes('records_officer')
  const qc = useQueryClient()
  const toast = useToast()
  const [editing, setEditing] = useState<MessageTemplateVersion | null>(null)
  const [editingKind, setEditingKind] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [changeReason, setChangeReason] = useState('')
  const [activationTarget, setActivationTarget] = useState<MessageTemplateVersion | null>(null)
  const [activationConfirmed, setActivationConfirmed] = useState(false)
  const [activationReason, setActivationReason] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  // A tripped guardrail is a decision to put in front of the officer, not an
  // error to toast away: the panel names what the requester loses and asks
  // for a reason before letting them publish anyway.
  const [guardrail, setGuardrail] = useState<GuardrailPrompt | null>(null)
  const [overrideReason, setOverrideReason] = useState('')

  const catalog = useQuery({
    queryKey: ['automatic-message-templates'],
    queryFn: () => api<Catalog>('/api/staff/automatic-message-templates/'),
  })

  const saveDraft = useMutation({
    mutationFn: (override: string) => {
      if (editing?.id) {
        return api<MessageTemplateVersion>(
          `/api/staff/automatic-message-templates/${editing.id}/`,
          {
            method: 'PATCH',
            body: {
              subject_template: subject,
              body_template: body,
              change_reason: changeReason,
              expected_version: editing.revision,
              guardrail_override_reason: override,
            },
          },
        )
      }
      return api<MessageTemplateVersion>('/api/staff/automatic-message-templates/', {
        method: 'POST',
        body: {
          kind: editingKind,
          subject_template: subject,
          body_template: body,
          change_reason: changeReason,
          guardrail_override_reason: override,
        },
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['automatic-message-templates'] })
      setEditing(null)
      setEditingKind('')
      setGuardrail(null)
      setOverrideReason('')
      toast('success', 'Template draft saved. Active automatic messages are unchanged.')
    },
    onError: (error) => {
      const prompt = guardrailPrompt(error)
      if (prompt) {
        setGuardrail(prompt)
        return
      }
      toast('error', error.message)
    },
  })

  const previewDraft = useMutation({
    mutationFn: (template: MessageTemplateVersion) => api<Preview>(
      `/api/staff/automatic-message-templates/${template.id}/preview/`,
      { method: 'POST', body: {} },
    ),
    onSuccess: (result) => setPreview(result),
    onError: (error) => toast('error', error.message),
  })

  const activate = useMutation({
    mutationFn: () => api<MessageTemplateVersion>(
      `/api/staff/automatic-message-templates/${activationTarget!.id}/activate/`,
      {
        method: 'POST',
        body: {
          expected_version: activationTarget!.revision,
          confirmed: true,
          reason: activationReason,
        },
      },
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['automatic-message-templates'] })
      setActivationTarget(null)
      setActivationConfirmed(false)
      setActivationReason('')
      toast('success', 'Template activated for future automatic messages.')
    },
    onError: (error) => toast('error', error.message),
  })

  function openEditor(slot: MessageTemplateSlot, template: MessageTemplateVersion | null) {
    const source = template ?? slot.active
    setEditing(template ?? { ...source, id: null, state: 'draft', revision: null })
    setEditingKind(slot.kind)
    setSubject(source.subject_template)
    setBody(source.body_template)
    setChangeReason('')
    setGuardrail(null)
    setOverrideReason('')
    saveDraft.reset()
  }

  function closeEditor() {
    setEditing(null)
    setEditingKind('')
    setGuardrail(null)
    setOverrideReason('')
    saveDraft.reset()
  }

  function submitDraft(event: FormEvent) {
    event.preventDefault()
    // Editing the wording clears a stale prompt: the officer may have just
    // fixed the thing it was warning about.
    setGuardrail(null)
    saveDraft.mutate('')
  }

  if (catalog.isPending) return <Spinner />
  if (catalog.isError) return <ErrorNote error={catalog.error} />

  return (
    <>
      <PageHeader
        title="Message templates"
        subtitle="Review the exact active content; draft and explicitly activate immutable new versions."
      />

      <div className="mb-6 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
        Editing wording never changes who may send a message. Structured
        templates produce the requester's message from validated case data, so
        an activated version is used for future drafts of that kind. Starting
        points are drafting aids only: you remain the author of that message.
        Required placeholders and statutory sentences cannot be removed. The
        submission receipt is a courtesy and is never the five-business-day
        RCW 42.56.520 response.
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        {catalog.data.templates.map((slot) => (
          <Card key={slot.kind} title={slot.label}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="green">Active version {slot.active.template_version}</Badge>
              <Badge tone={slot.authoring === 'structured' ? 'blue' : 'neutral'}>
                {slot.authoring === 'structured'
                  ? 'Structured template'
                  : 'Starting point'}
              </Badge>
              <span className="text-xs text-pacific/80">{slot.group}</span>
              {slot.active.source === 'built_in' ? (
                <span className="text-xs text-pacific/80">Built-in application default</span>
              ) : (
                <span className="text-xs text-pacific/80">
                  Activated by {slot.active.activated_by_username ?? 'records officer'}
                </span>
              )}
            </div>
            <p className="mt-4 font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
              Subject
            </p>
            <pre className="mt-1 whitespace-pre-wrap rounded-md border border-driftwood/50 bg-skylight/60 p-3 text-sm text-pacific">
              {slot.active.subject_template}
            </pre>
            <p className="mt-4 font-heading text-xs font-bold uppercase tracking-wide text-pacific/80">
              Body
            </p>
            <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-driftwood/50 bg-skylight/60 p-3 text-sm text-pacific">
              {slot.active.body_template}
            </pre>
            <p className="mt-3 text-xs text-pacific/80">
              Required placeholders: {slot.required_tokens.map((token) => `{{ ${token} }}`).join(' · ')}
            </p>

            {slot.drafts.length > 0 && (
              <div className="mt-5 space-y-3 border-t border-driftwood/50 pt-4">
                <p className="font-heading text-sm font-bold text-pacific">Unactivated drafts</p>
                {slot.drafts.map((draft) => (
                  <div key={draft.id} className="rounded-md border border-driftwood/60 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="font-heading font-semibold text-pacific">
                          Version {draft.template_version}
                        </p>
                        <p className="text-xs text-pacific/80">{draft.change_reason}</p>
                      </div>
                      {canEdit && (
                        <div className="flex flex-wrap gap-2">
                          <Button type="button" variant="ghost" onClick={() => openEditor(slot, draft)}>
                            Edit draft
                          </Button>
                          <Button type="button" variant="ghost" onClick={() => previewDraft.mutate(draft)}>
                            Preview example
                          </Button>
                          <Button
                            type="button"
                            onClick={() => {
                              setActivationTarget(draft)
                              setActivationConfirmed(false)
                              setActivationReason('')
                            }}
                          >
                            Activate
                          </Button>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {canEdit && (
              <Button className="mt-5" type="button" onClick={() => openEditor(slot, null)}>
                Create editable version
              </Button>
            )}
          </Card>
        ))}
      </div>

      <Modal open={editing !== null} title="Edit automatic message template" onClose={closeEditor}>
        <form onSubmit={submitDraft} className="space-y-4">
          <p className="text-sm text-pacific/80">
            Saving creates or updates a draft only. It cannot affect queued,
            approved, sent, or future messages until separately activated.
          </p>
          <Field label="Subject template">
            <Input value={subject} onChange={(event) => setSubject(event.target.value)} required />
          </Field>
          <Field label="Body template">
            <Textarea
              rows={15}
              value={body}
              onChange={(event) => setBody(event.target.value)}
              required
            />
          </Field>
          <Field
            label="Change reason"
            hint={reasonHint('explain why this wording is changing')}
          >
            <Textarea
              rows={2}
              value={changeReason}
              onChange={(event) => setChangeReason(event.target.value)}
              minLength={MIN_REASON_LENGTH}
              required
            />
          </Field>
          {reasonShortfall(changeReason) && (
            <p aria-live="polite" className="text-sm text-pacific/80">
              {reasonShortfall(changeReason)}
            </p>
          )}
          {guardrail && (
            <div
              className="rounded-md border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-950"
              aria-live="polite"
            >
              <p className="font-heading font-bold">
                {guardrail.mayOverride
                  ? 'Check what this wording means for the requester'
                  : 'This wording needs a Records Officer'}
              </p>
              <ul className="mt-2 space-y-2">
                {guardrail.warnings.map((warning) => (
                  <li key={warning.code}>
                    <span className="font-semibold">{warning.message}</span>{' '}
                    {warning.consequence}
                  </li>
                ))}
              </ul>
              {guardrail.mayOverride ? (
                <div className="mt-3 space-y-2">
                  <Field
                    label="Why publish it anyway?"
                    hint={reasonHint('this is kept with the template version')}
                  >
                    <Textarea
                      rows={2}
                      value={overrideReason}
                      onChange={(event) => setOverrideReason(event.target.value)}
                      minLength={MIN_REASON_LENGTH}
                    />
                  </Field>
                  {reasonShortfall(overrideReason) && (
                    <p aria-live="polite">{reasonShortfall(overrideReason)}</p>
                  )}
                  <Button
                    type="button"
                    disabled={
                      saveDraft.isPending || reasonTooShort(overrideReason)
                    }
                    onClick={() => saveDraft.mutate(overrideReason)}
                  >
                    Save draft anyway
                  </Button>
                </div>
              ) : (
                <p className="mt-2">{guardrail.detail}</p>
              )}
            </div>
          )}
          {saveDraft.isError && !guardrail && (
            <ErrorNote error={saveDraft.error} />
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeEditor}>Cancel</Button>
            <Button
              type="submit"
              disabled={saveDraft.isPending || reasonTooShort(changeReason)}
            >
              {saveDraft.isPending ? 'Saving…' : 'Save draft'}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal open={preview !== null} title="Rendered example" onClose={() => setPreview(null)}>
        {preview && (
          <div className="space-y-4">
            <div>
              <p className="font-heading text-xs font-bold uppercase text-pacific/80">Subject</p>
              <p className="mt-1 text-sm text-pacific">{preview.subject}</p>
            </div>
            <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-md border border-driftwood/60 bg-skylight/60 p-3 text-sm text-pacific">
              {preview.body}
            </pre>
            <div className="flex justify-end">
              <Button type="button" onClick={() => setPreview(null)}>Close</Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={activationTarget !== null}
        title="Activate automatic message template"
        onClose={() => setActivationTarget(null)}
      >
        <div className="space-y-4">
          <p className="text-sm text-pacific/80">
            Activation makes this exact version immutable and uses it for future
            automatic messages. Existing drafts and delivery snapshots do not change.
          </p>
          <Field
            label="Activation reason"
            hint={reasonHint('this version becomes immutable once activated')}
          >
            <Textarea
              rows={2}
              value={activationReason}
              onChange={(event) => setActivationReason(event.target.value)}
              minLength={MIN_REASON_LENGTH}
              required
            />
          </Field>
          {reasonShortfall(activationReason) && (
            <p aria-live="polite" className="text-sm text-pacific/80">
              {reasonShortfall(activationReason)}
            </p>
          )}
          <label className="flex items-start gap-2 text-sm text-pacific">
            <input
              className="mt-1"
              type="checkbox"
              checked={activationConfirmed}
              onChange={(event) => setActivationConfirmed(event.target.checked)}
            />
            <span>I reviewed this exact subject and body and intend to use it for future automatic messages.</span>
          </label>
          {activate.isError && <ErrorNote error={activate.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setActivationTarget(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={
                activate.isPending
                || !activationConfirmed
                || reasonTooShort(activationReason)
              }
              onClick={() => activate.mutate()}
            >
              {activate.isPending ? 'Activating…' : 'Activate version'}
            </Button>
          </div>
        </div>
      </Modal>
    </>
  )
}
