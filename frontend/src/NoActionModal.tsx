// ABOUTME: The "no action required" dialog, shared by My Work and the message workspace.
// ABOUTME: Both rendered a byte-identical copy; they drifted the moment either was edited.
import type { FormEvent } from 'react'
import {
  Button,
  ErrorNote,
  Field,
  MIN_REASON_LENGTH,
  Modal,
  Select,
  Textarea,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
} from './ui'

const NO_ACTION_CATEGORIES = [
  { value: 'courtesy_copy', label: 'Courtesy copy' },
  { value: 'duplicate_notification', label: 'Duplicate notification' },
  { value: 'informational_only', label: 'Informational only' },
  { value: 'resolved_elsewhere', label: 'Resolved elsewhere' },
  { value: 'superseded', label: 'Superseded' },
  { value: 'not_a_records_request', label: 'Not a records request' },
] as const

export function NoActionModal({
  open,
  onClose,
  onSubmit,
  category,
  onCategoryChange,
  reason,
  onReasonChange,
  pending,
  error,
}: {
  open: boolean
  onClose: () => void
  onSubmit: () => void
  category: string
  onCategoryChange: (value: string) => void
  reason: string
  onReasonChange: (value: string) => void
  pending: boolean
  error: unknown
}) {
  const shortfall = reasonShortfall(reason)
  return (
    <Modal
      open={open}
      title="Record no action required"
      onClose={onClose}
      dismissible={!pending}
    >
      <form
        className="space-y-4"
        onSubmit={(event: FormEvent) => {
          event.preventDefault()
          onSubmit()
        }}
      >
        <Field label="No-action category">
          <Select value={category} onChange={(event) => onCategoryChange(event.target.value)}>
            {NO_ACTION_CATEGORIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="Why no team action is required"
          hint={reasonHint('this is recorded in the audit trail')}
        >
          <Textarea
            minLength={MIN_REASON_LENGTH}
            maxLength={2000}
            rows={4}
            required
            value={reason}
            onChange={(event) => onReasonChange(event.target.value)}
          />
        </Field>
        {shortfall && (
          <p aria-live="polite" className="text-sm text-pacific/80">
            {shortfall}
          </p>
        )}
        {error != null && <ErrorNote error={error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button type="submit" disabled={pending || reasonTooShort(reason)}>
            {pending ? 'Recording…' : 'Record no action'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
