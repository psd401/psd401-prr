// ABOUTME: The case's internal staff notes — compose one, read the rest.
// ABOUTME: Internal to the district and append-only; nothing here is sent.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api, unwrap, type Page } from '../api/client'
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Spinner,
  Textarea,
  useToast,
} from '../ui'

/** Mirrors ``CASE_NOTE_MAX_LENGTH`` in apps/records/models.py. */
const NOTE_MAX_LENGTH = 10_000

export interface CaseNote {
  id: string
  body: string
  author_username: string | null
  created_at: string
}

function noteTimestamp(value: string) {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

/**
 * What staff told each other about this case, oldest question first.
 *
 * Notes are internal: nothing written here is rendered into a letter, a
 * production, or an export, which the backend asserts against the real
 * drafting path. They are also append-only, so the composer says so rather
 * than offering an edit affordance that would only fail.
 *
 * Internal is not secret, and the hint says that too: a note about a public
 * records request is very likely itself a disclosable public record. Better
 * that everyone writing one knows it than that they find out later.
 */
export function CaseNotesPanel({
  number,
  className = '',
}: {
  number: string
  className?: string
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState('')

  const notes = useQuery({
    queryKey: ['case-notes', number],
    queryFn: () =>
      api<Page<CaseNote> | CaseNote[]>(
        `/api/staff/requests/${number}/notes/`,
        { fetchAllPages: false },
      ),
    enabled: !!number,
  })

  const add = useMutation({
    mutationFn: (body: string) =>
      api<CaseNote>(`/api/staff/requests/${number}/notes/`, {
        method: 'POST',
        body: { body },
      }),
    onSuccess: async () => {
      setDraft('')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['case-notes', number] }),
        // The case list carries the newest note in a column of its own.
        queryClient.invalidateQueries({ queryKey: ['inbox-cases'] }),
      ])
      toast('success', 'Note added.')
    },
    onError: (error) => toast('error', error.message),
  })

  const trimmed = draft.trim()
  // A payload that is neither a page nor a list is a bug somewhere else, and
  // the case page still has to render: an empty panel beside the record is
  // recoverable, a blank screen over the whole case is not.
  const rows = (notes.data ? unwrap(notes.data) : undefined) ?? []

  return (
    <Card title="Internal notes" className={className}>
      <form
        className="space-y-2"
        onSubmit={(event: FormEvent) => {
          event.preventDefault()
          if (!trimmed) return
          add.mutate(trimmed)
        }}
      >
        <Textarea
          aria-label="Add an internal note"
          rows={3}
          maxLength={NOTE_MAX_LENGTH}
          value={draft}
          placeholder="What you did, who you spoke to, what you are waiting on."
          onChange={(event) => setDraft(event.target.value)}
        />
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!trimmed || add.isPending}>
            Add note
          </Button>
          <p className="text-xs text-pacific/80">
            Staff only — never sent to the requester. Notes cannot be edited or
            deleted, and may themselves be disclosable public records.
          </p>
        </div>
      </form>

      <div className="mt-4">
        {notes.isPending && <Spinner label="Loading notes…" />}
        {notes.isError && <ErrorNote error={notes.error} />}
        {notes.data && rows.length === 0 && (
          <EmptyState>No notes on this case yet.</EmptyState>
        )}
        {rows.length > 0 && (
          <ul className="space-y-3">
            {rows.map((note) => (
              <li
                key={note.id}
                className="rounded-lg border border-driftwood/60 bg-white p-3"
              >
                <p className="font-heading text-xs font-semibold uppercase tracking-[0.06em] text-pacific/80">
                  {note.author_username ?? 'Staff'}
                  {' · '}
                  <time dateTime={note.created_at}>
                    {noteTimestamp(note.created_at)}
                  </time>
                </p>
                <p className="mt-1 whitespace-pre-wrap text-sm text-pacific">
                  {note.body}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  )
}
