// ABOUTME: Application-admin screen for every setting the district manages in
// ABOUTME: the app — consequence, current value, reasoned change, and history.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import {
  EmptyState,
  Badge,
  Button,
  Card,
  ErrorNote,
  Field,
  Input,
  MIN_REASON_LENGTH,
  Modal,
  Select,
  Spinner,
  Textarea,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
  useToast,
} from '../ui'
import { formatMoment } from '../date'

/**
 * What a write-only setting is allowed to say about itself: whether one is
 * installed, a fingerprint to compare against the value in hand, and who
 * installed it. Never the value — there is no read path, for anyone.
 */
interface SecretState {
  set: boolean
  fingerprint: string
  set_by: string
  set_at: string | null
}

/** Whatever a setting's type makes it: a switch, a number, a string, a list. */
type SettingValue = boolean | number | string | string[] | SecretState

interface SettingChange {
  id: number
  at: string
  key: string
  old_value: SettingValue | null
  new_value: SettingValue
  reason: string
  source: string
  actor_email: string
  actor_name: string
}

/** The result of the last live check of a credential, when it has one. */
interface VerificationState {
  ok: boolean
  detail: string
  checked_at: string
}

interface DistrictSetting {
  key: string
  label: string
  type: string
  category: string
  consequence: string
  value: SettingValue
  default: SettingValue | null
  unit: string
  minimum: number | null
  maximum: number | null
  periods: string[]
  max_length: number | null
  max_items: number | null
  allow_blank: boolean
  choices: string[]
  /** Known good values offered as a pick list (the granted AI models). */
  suggestions?: string[]
  requires: string[]
  required_by: string[]
  last_change: SettingChange | null
  verification: VerificationState | null
  evaluation: EvaluationState | null
}

/** How the live model scored on its latest evaluation (#34). */
interface EvaluationState {
  model: string
  evaluated_at: string
  items: number | null
  precision: number | null
  recall: number | null
}

interface SettingsPage {
  categories: string[]
  reason_min_length: number
  results: DistrictSetting[]
}

const INTAKE_TRUST_KEYS = new Set([
  'PRR_TRUST_INTAKE_SPAM_MODE',
  'PRR_TRUST_INTAKE_NON_REQUEST_MODE',
])

/**
 * The switches that decide how cases are worked, shown first. Everything else
 * (limits, timeouts, transport plumbing) sits under Advanced.
 */
const MAIN_KEYS = [
  'PRR_AI_ADVISORY_ENABLED',
  'PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED',
  'PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED',
  'PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED',
  'PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED',
  'AUTO_ACK_SAFETY_NET',
  'PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED',
  'PRR_AI_MODEL',
  'PRR_AI_MODEL_INTAKE',
  'PRR_TRUST_INTAKE_SPAM_MODE',
  'PRR_TRUST_INTAKE_NON_REQUEST_MODE',
  'PRR_TRUST_SCOPE_MODE',
  'PRR_TRUST_RESPONSIVE_MODE',
  'PRR_TRUST_NON_RESPONSIVE_MODE',
  'PRR_TRUST_REDACTION_MODE',
]

function matches(setting: DistrictSetting, query: string) {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  return [setting.label, setting.consequence, setting.key, setting.category]
    .some((text) => text.toLowerCase().includes(needle))
}

const ADVANCED_OPEN_KEY = 'prr.settings.advancedOpen'

const LIST_TYPES = ['domain_list', 'email_list', 'date_list']

/** What one entry of a list setting is, as the hint names it. */
const LIST_ITEMS: Record<string, string> = {
  domain_list: 'domains',
  email_list: 'email addresses',
  date_list: 'dates in the form YYYY-MM-DD',
}

function isList(setting: { type: string }): boolean {
  return LIST_TYPES.includes(setting.type)
}

function isSecret(setting: { type: string }): boolean {
  return setting.type === 'secret'
}

function secretState(setting: DistrictSetting): SecretState {
  return setting.value as SecretState
}

function formatWhen(value: string): string {
  return formatMoment(value)
}

/**
 * Grouping digits is the difference between a reader seeing "500 MiB" and a
 * reader seeing a wall of zeroes they will not check. Byte limits are shown
 * in the unit they were written in as well as exactly, because an admin
 * typing a new one needs the exact number.
 */
function formatBytes(value: number): string {
  const units = [
    ['GiB', 1024 ** 3],
    ['MiB', 1024 ** 2],
    ['KiB', 1024],
  ] as const
  for (const [name, size] of units) {
    if (value >= size && value % size === 0) return `${value / size} ${name}`
  }
  return `${value.toLocaleString()} bytes`
}

/** The current value, as a person reads it. */
function displayValue(setting: DistrictSetting): string {
  if (isSecret(setting)) {
    return secretState(setting).set ? 'Set' : 'Not set'
  }
  if (setting.type === 'boolean') return setting.value ? 'On' : 'Off'
  if (isList(setting)) {
    const items = setting.value as string[]
    return items.length > 0 ? items.join(', ') : 'None'
  }
  if (setting.type === 'integer') {
    const numeric = setting.value as number
    if (setting.unit === 'bytes') return formatBytes(numeric)
    return setting.unit
      ? `${numeric.toLocaleString()} ${setting.unit}`
      : numeric.toLocaleString()
  }
  return (setting.value as string) || 'Not set'
}

/** The same, for a history row, which has only the raw value to work with. */
function displayChangeValue(value: SettingValue | null): string {
  if (value === null) return 'unset'
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (Array.isArray(value)) return value.length > 0 ? value.join(', ') : 'none'
  if (typeof value === 'number') return value.toLocaleString()
  if (typeof value === 'object') {
    // A secret's history row carries what it is allowed to carry: whether a
    // value is installed and which one, by fingerprint.
    return value.set ? `set (${value.fingerprint})` : 'cleared'
  }
  return value === '' ? 'blank' : value
}

/** What an admin needs to know before typing, in one sentence. */
function editorHint(setting: DistrictSetting): string {
  if (isSecret(setting)) {
    const clearing = setting.allow_blank
      ? ' Leave it empty to clear the secret entirely.'
      : ''
    return (
      'The new value, at most '
      + `${setting.max_length} characters. It is stored encrypted and cannot `
      + `be read back — not on this page, and not by you.${clearing}`
    )
  }
  if (setting.suggestions && setting.suggestions.length > 0) {
    return 'Pick one of the AI models the district has approved.'
  }
  if (setting.type === 'choice') {
    return `One of: ${setting.choices.join(', ')}.`
  }
  if (setting.type === 'integer' && setting.unit === 'bytes') {
    return `Between ${formatBytes(setting.minimum ?? 0)} and ${formatBytes(setting.maximum ?? 0)}, entered in bytes.`
  }
  if (setting.type === 'integer') {
    const bounds = `Between ${setting.minimum?.toLocaleString()} and ${setting.maximum?.toLocaleString()}`
    const unit = setting.unit ? ` ${setting.unit}` : ''
    return `${bounds}${unit}.`
  }
  if (setting.type === 'rate') {
    return `A rate such as 10/hour, using one of: ${setting.periods.join(', ')}.`
  }
  if (isList(setting)) {
    const what = LIST_ITEMS[setting.type]
    const blank = setting.allow_blank ? ' Leave blank for none.' : ''
    return `Comma-separated ${what}, at most ${setting.max_items}.${blank}`
  }
  if (setting.type === 'domain') return 'One exact DNS domain, such as psd401.net.'
  if (setting.type === 'email') return 'One email address.'
  const blank = setting.allow_blank ? ' May be left blank.' : ''
  return `At most ${setting.max_length} characters.${blank}`
}

/** The draft an editor starts from: the current value, as text. */
function draftFrom(setting: DistrictSetting): string {
  // A secret starts empty, always. There is nothing to pre-fill it with,
  // and an editor that looked pre-filled would invite an administrator to
  // save a value they never typed.
  if (isSecret(setting)) return ''
  if (isList(setting)) return (setting.value as string[]).join(', ')
  if (setting.type === 'integer') return String(setting.value)
  return String(setting.value ?? '')
}

/** The draft on its way back, in the shape the API expects. */
function parseDraft(setting: DistrictSetting, draft: string): SettingValue {
  // A secret is sent exactly as typed apart from surrounding whitespace: it
  // is not lower-cased, not split, not normalised in any way a value the
  // server has to match byte for byte could survive.
  if (isSecret(setting)) return draft.trim()
  if (isList(setting)) {
    return draft
      .split(',')
      .map((item) => item.trim())
      .filter((item) => item.length > 0)
  }
  if (setting.type === 'integer') return Number(draft.trim())
  return draft.trim()
}

/**
 * Why this draft cannot be sent yet, or null.
 *
 * The only rules checked here are the ones the row itself carries — a number
 * must be a whole number inside the bounds the server sent, a required field
 * must not be blank. That is not a second copy of the registry: it is the
 * same copy, transported, which is why the page may act on it. Everything the
 * row does not describe (the domain grammar, the rate periods, the
 * reply-address pairing, the ordering relations) is left to the store, and its
 * refusal is shown verbatim.
 *
 * The reason for checking the bounds here at all is accessibility. `min` and
 * `max` on a number input make the browser block the submit with a transient
 * native bubble; saying it ourselves puts the limit in a persistent, focused
 * alert the administrator can actually read.
 */
function draftProblem(setting: DistrictSetting, draft: string): string | null {
  const text = draft.trim()
  if (isSecret(setting)) {
    // Empty means "clear it", which is only a change when one is installed.
    if (text === '' && !secretState(setting).set) {
      return 'Enter the new value.'
    }
    return null
  }
  if (setting.type === 'integer') {
    const parsed = Number(text)
    if (text === '' || !Number.isInteger(parsed)) {
      return 'Enter a whole number.'
    }
    const low = setting.minimum ?? Number.NEGATIVE_INFINITY
    const high = setting.maximum ?? Number.POSITIVE_INFINITY
    if (parsed < low || parsed > high) return editorHint(setting)
    return null
  }
  if (isList(setting) || setting.allow_blank) return null
  return text === '' ? 'This setting may not be left blank.' : null
}

/**
 * What an installed secret can be said about. The fingerprint is the only
 * way to answer "is the value in front of me the one that is installed?"
 * without a read path, so it is shown rather than hidden as noise.
 */
function SecretSummary({ setting }: { setting: DistrictSetting }) {
  const state = secretState(setting)
  if (!state.set) {
    return (
      <p className="mt-2 text-xs text-ink-muted">
        No value is installed. The feature that needs it stays off.
      </p>
    )
  }
  return (
    <p className="mt-2 text-xs text-ink-muted">
      {`Installed, fingerprint ${state.fingerprint}`}
      {state.set_by ? ` — set by ${state.set_by}` : ''}
      {state.set_at ? ` on ${formatWhen(state.set_at)}` : ''}
      {'. The value cannot be read back.'}
    </p>
  )
}

/** Saved is not the same as working, and the row says which. */
function VerificationNote({ setting }: { setting: DistrictSetting }) {
  const check = setting.verification
  if (!check) return null
  if (check.ok) {
    return (
      <p className="mt-2 text-xs text-ink-muted">
        {`Verified against Google on ${formatWhen(check.checked_at)}.`}
      </p>
    )
  }
  return (
    <p className="mt-2 text-xs text-warning-ink">
      {`Saved, but it did not authenticate when it was last checked on `}
      {formatWhen(check.checked_at)}
      {`. ${check.detail}`}
    </p>
  )
}

function percent(value: number | null): string {
  return value === null ? 'not measured' : `${Math.round(value * 100)}%`
}

/** The evidence for a setting that lets the system act without a person. */
function EvaluationNote({ setting }: { setting: DistrictSetting }) {
  const result = setting.evaluation
  if (!result) {
    return (
      <p className="mt-2 text-xs text-warning-ink">
        The live intake model has not been evaluated yet. Run an evaluation
        before choosing Auto.
      </p>
    )
  }
  return (
    <p className="mt-2 text-xs text-ink-muted">
      {`Latest evaluation of ${result.model} on ${formatWhen(result.evaluated_at)}`}
      {result.items !== null ? ` (${result.items} cases)` : ''}
      {`: “not a request” precision ${percent(result.precision)}, `}
      {`recall ${percent(result.recall)}. Spam and not-a-request are scored together.`}
    </p>
  )
}

function changedBy(change: SettingChange): string {
  if (change.source === 'system_import') return 'System import'
  return change.actor_name || change.actor_email || 'Unknown'
}

function ChangeLine({ change }: { change: SettingChange }) {
  return (
    <li className="border-t border-line py-2 text-sm text-ink-muted">
      <span className="font-heading font-semibold text-ink">
        {`Changed to ${displayChangeValue(change.new_value)}`}
      </span>
      {` from ${displayChangeValue(change.old_value)} by `}
      {changedBy(change)}
      {' on '}
      {formatWhen(change.at)}
      <span className="mt-1 block">{change.reason}</span>
    </li>
  )
}

/**
 * The full append-only history of one setting, fetched only when an admin
 * asks. The list response carries just the latest change: a page that
 * eagerly loaded every history would grow without bound for the one setting
 * that gets changed repeatedly during an incident.
 */
function History({ settingKey }: { settingKey: string }) {
  const history = useQuery({
    queryKey: ['district-settings', settingKey, 'history'],
    queryFn: () => api<{ key: string; results: SettingChange[] }>(
      `/api/staff/settings/${settingKey}/history/`,
      { fetchAllPages: false },
    ),
  })
  if (history.isPending) return <Spinner label="Loading history…" />
  if (history.isError) return <ErrorNote error={history.error} />
  if (history.data.results.length === 0) {
    return (
      <p className="mt-3 border-t border-line py-2 text-sm text-ink-muted">
        No recorded changes yet.
      </p>
    )
  }
  return (
    <ul className="mt-3">
      {history.data.results.map((change) => (
        <ChangeLine key={change.id} change={change} />
      ))}
    </ul>
  )
}

function ValueEditor({
  setting,
  draft,
  onChange,
}: {
  setting: DistrictSetting
  draft: string
  onChange: (next: string) => void
}) {
  if (isSecret(setting)) {
    return (
      <Input
        type="password"
        autoComplete="off"
        spellCheck={false}
        maxLength={setting.max_length ?? undefined}
        value={draft}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  if (setting.suggestions && setting.suggestions.length > 0) {
    const options = setting.suggestions.includes(draft) || draft === ''
      ? setting.suggestions
      : [draft, ...setting.suggestions]
    return (
      <Select
        className="w-full"
        value={draft}
        onChange={(event) => onChange(event.target.value)}
      >
        {setting.allow_blank && <option value="">Not set</option>}
        {options.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </Select>
    )
  }
  if (setting.type === 'choice') {
    return (
      <Select
        className="w-full"
        value={draft}
        onChange={(event) => onChange(event.target.value)}
      >
        {setting.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </Select>
    )
  }
  if (isList(setting)) {
    return (
      <Textarea
        rows={3}
        value={draft}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  if (setting.type === 'integer') {
    return (
      <Input
        type="number"
        inputMode="numeric"
        step={1}
        min={setting.minimum ?? undefined}
        max={setting.maximum ?? undefined}
        value={draft}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  return (
    <Input
      type="text"
      maxLength={setting.max_length ?? undefined}
      value={draft}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

export function DistrictSettings() {
  const qc = useQueryClient()
  const toast = useToast()
  const [pending, setPending] = useState<DistrictSetting | null>(null)
  const [reason, setReason] = useState('')
  const [draft, setDraft] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  // Remembered per browser, so a reload keeps the section as it was left.
  const [advancedOpen, setAdvancedOpenState] = useState(() => {
    try {
      return window.localStorage.getItem(ADVANCED_OPEN_KEY) === 'true'
    } catch {
      return false
    }
  })
  const setAdvancedOpen = (open: boolean) => {
    setAdvancedOpenState(open)
    try {
      window.localStorage.setItem(ADVANCED_OPEN_KEY, String(open))
    } catch {
      // Storage refused (private window): the choice lasts this visit only.
    }
  }

  const list = useQuery({
    queryKey: ['district-settings'],
    // One response carries every setting: the registry is a fixed, bounded
    // list by design, so this page never pages.
    queryFn: () => api<SettingsPage>('/api/staff/settings/', {
      fetchAllPages: false,
    }),
  })

  const save = useMutation({
    mutationFn: (input: {
      setting: DistrictSetting
      value: SettingValue
      reason: string
    }) =>
      api<DistrictSetting>(`/api/staff/settings/${input.setting.key}/`, {
        method: 'PATCH',
        body: { value: input.value, reason: input.reason },
      }),
    onSuccess: (result, input) => {
      qc.invalidateQueries({ queryKey: ['district-settings'] })
      toast(
        'success',
        isSecret(result)
          ? (secretState(result).set
            ? `${input.setting.label} is installed. It cannot be read back.`
            : `${input.setting.label} has been cleared.`)
          : `${input.setting.label} is now ${displayValue(result)}.`,
      )
      close()
    },
    onError: (error) => toast('error', error.message),
  })

  function open(setting: DistrictSetting) {
    setPending(setting)
    setReason('')
    setDraft(draftFrom(setting))
    save.reset()
  }

  /** A refusal describes the value that was sent; editing that value retires it. */
  function editDraft(next: string) {
    setDraft(next)
    if (save.isError) save.reset()
  }

  function close() {
    setPending(null)
    setReason('')
    setDraft('')
    save.reset()
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!pending || reasonTooShort(reason)) return
    if (pending.type !== 'boolean' && draftProblem(pending, draft)) return
    const value = pending.type === 'boolean'
      ? !pending.value
      : parseDraft(pending, draft)
    save.mutate({ setting: pending, value, reason })
  }

  if (list.isPending) return <Spinner />
  if (list.isError) return <ErrorNote error={list.error} />

  const page = list.data
  const searching = query.trim() !== ''
  const shown = page.results.filter((row) => matches(row, query))
  const main = MAIN_KEYS
    .map((key) => shown.find((row) => row.key === key))
    .filter((row): row is DistrictSetting => row !== undefined)
  const byCategory = page.categories
    .map((category) => ({
      category,
      settings: shown.filter(
        (row) => row.category === category && !MAIN_KEYS.includes(row.key),
      ),
    }))
    .filter((group) => group.settings.length > 0)
  const renderList = (settings: DistrictSetting[]) => (
    <ul className="divide-y divide-line">
      {settings.map((setting) => (
        <li key={setting.key} className="py-4">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-heading font-bold text-ink">
                  {setting.label}
                </span>
                {setting.type === 'boolean'
                  ? (setting.value
                    ? <Badge tone="green">On</Badge>
                    : <Badge>Off</Badge>)
                  : <Badge>{displayValue(setting)}</Badge>}
                {setting.verification && !setting.verification.ok && (
                  <Badge tone="amber">Unverified</Badge>
                )}
              </div>
              <p className="mt-2 text-sm leading-relaxed text-ink-muted">
                {setting.consequence}
              </p>
              {isSecret(setting) && <SecretSummary setting={setting} />}
              <VerificationNote setting={setting} />
              {INTAKE_TRUST_KEYS.has(setting.key) && <EvaluationNote setting={setting} />}
              <p className="mt-2 text-xs text-ink-muted">
                {setting.last_change
                  ? `Last changed by ${changedBy(setting.last_change)} on `
                    + `${formatWhen(setting.last_change.at)} — `
                    + setting.last_change.reason
                  : 'Never changed in the application.'}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                variant="ghost"
                aria-expanded={expanded === setting.key}
                onClick={() => setExpanded(
                  expanded === setting.key ? null : setting.key,
                )}
              >
                History
              </Button>
              <Button
                variant={
                  setting.type === 'boolean' && setting.value
                    ? 'danger'
                    : 'primary'
                }
                onClick={() => open(setting)}
              >
                {isSecret(setting)
                  ? (secretState(setting).set ? 'Replace' : 'Set')
                  : setting.type !== 'boolean'
                    ? 'Change'
                    : setting.value ? 'Turn off' : 'Turn on'}
              </Button>
            </div>
          </div>
          {expanded === setting.key && (
            <History settingKey={setting.key} />
          )}
        </li>
      ))}
    </ul>
  )

  const shortfall = reasonShortfall(reason)
  const isSwitch = pending?.type === 'boolean'
  const problem = pending !== null && !isSwitch
    ? draftProblem(pending, draft)
    : null

  return (
    <>
      <PageHeader
        title="District settings"
        subtitle="Changes take effect within seconds. Each needs a reason and is kept in its history."
      />
      <Input
        type="search"
        aria-label="Search settings"
        placeholder="Search settings"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        className="mb-6 w-full"
      />
      {searching && shown.length === 0 && (
        <EmptyState>No setting matches that search.</EmptyState>
      )}
      {main.length > 0 && (
        <Card title="Main" className="mb-6">
          {renderList(main)}
        </Card>
      )}
      {byCategory.length > 0 && (
        <details
          open={searching || advancedOpen}
          onToggle={(event) => {
            // A search opens the section by itself; only a person's click is
            // remembered, so saving a change does not fold it shut again.
            if (!searching) setAdvancedOpen(event.currentTarget.open)
          }}
          className="mb-6"
        >
          <summary className="mb-4 cursor-pointer font-heading text-lg font-bold text-ink">
            Advanced settings
          </summary>
          {byCategory.map((group) => (
            <Card key={group.category} title={group.category} className="mb-6">
              {renderList(group.settings)}
            </Card>
          ))}
        </details>
      )}

      <Modal
        open={pending !== null}
        title={
          pending
            ? (isSwitch
              ? `${pending.value ? 'Turn off' : 'Turn on'} ${pending.label}`
              : isSecret(pending)
                ? `${secretState(pending).set ? 'Replace' : 'Set'} ${pending.label}`
                : `Change ${pending.label}`)
            : ''
        }
        onClose={close}
        dismissible={!save.isPending}
      >
        {pending && (
          <form onSubmit={submit} className="space-y-4">
            <p className="text-sm leading-relaxed text-ink-muted">
              {pending.consequence}
            </p>
            {!isSwitch && (
              <>
                <Field label="New value" hint={editorHint(pending)}>
                  <ValueEditor
                    setting={pending}
                    draft={draft}
                    onChange={editDraft}
                  />
                </Field>
                {problem && (
                  <p
                    role="alert"
                    className="rounded-md border border-danger bg-danger-soft px-3 py-2 text-sm text-danger-ink"
                  >
                    {problem}
                  </p>
                )}
              </>
            )}
            <Field
              label="Why this is changing"
              hint={reasonHint('this is kept permanently in the setting’s history')}
            >
              <Textarea
                minLength={MIN_REASON_LENGTH}
                maxLength={2000}
                rows={4}
                required
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </Field>
            {shortfall && (
              <p aria-live="polite" className="text-sm text-ink-muted">
                {shortfall}
              </p>
            )}
            {save.isError && <ErrorNote error={save.error} />}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={close}
                disabled={save.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant={isSwitch && pending.value ? 'danger' : 'primary'}
                disabled={
                  save.isPending || reasonTooShort(reason) || problem !== null
                }
              >
                {save.isPending
                  ? 'Saving…'
                  : isSwitch
                    ? (pending.value ? 'Turn off' : 'Turn on')
                    : 'Save change'}
              </Button>
            </div>
          </form>
        )}
      </Modal>
    </>
  )
}
