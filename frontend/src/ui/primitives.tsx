// ABOUTME: Small Nexus UI kit — buttons, inputs, cards, badges — shared by every
// ABOUTME: screen so the app reads as one system. Styles are the vendored nx- classes.
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'
import { useEffect, useRef } from 'react'

// Nexus has one outlined button; `ghost` is the same control without a fill,
// for toolbars where a raised surface would be noise.
const buttonStyles = {
  primary: 'nx-btn--primary',
  secondary: 'nx-btn--secondary',
  ghost: 'nx-btn--secondary bg-transparent',
  danger: 'nx-btn--danger',
} as const

/**
 * One filter pill for every screen. Previously each list hand-rolled its own,
 * in two different visual styles, so the same control looked different
 * depending on which page you were on.
 */
export function FilterChip({
  selected,
  count,
  countLabel,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  selected: boolean
  /** Optional badge, e.g. how many items the filter would show. */
  count?: number
  /** Spoken form of `count`; a bare number reads as meaningless to a screen reader. */
  countLabel?: string
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      // nx-chip is Nexus's context chip (a pinned decision), not a filter, so
      // the filter pill stays a pill drawn from the same tokens.
      className={`rounded-full border px-3 py-1.5 font-heading text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ${
        selected
          ? 'border-action bg-action text-on-action'
          : 'border-line-strong bg-surface-raised text-ink hover:bg-surface-hover'
      }`}
      {...props}
    >
      {children}
      {count !== undefined && (
        <span
          aria-label={countLabel}
          className="ml-2 rounded-full bg-current/10 px-1.5 py-0.5 text-xs tabular-nums"
        >
          <span aria-hidden="true">{count}</span>
        </span>
      )}
    </button>
  )
}

export function Button({
  variant = 'primary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: keyof typeof buttonStyles
}) {
  return (
    <button
      // Nexus buttons are one line at a fixed height; these labels can be long
      // and must wrap at 320 CSS px rather than widen the page.
      className={`nx-btn ${buttonStyles[variant]} h-auto min-h-10 whitespace-normal py-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ${className}`}
      {...props}
    />
  )
}

export function Input({
  className = '',
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`nx-input ${className}`}
      {...props}
    />
  )
}

export function Textarea({
  className = '',
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={`nx-textarea ${className}`}
      {...props}
    />
  )
}

export function Select({
  className = '',
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`nx-select max-w-full ${className}`}
      {...props}
    />
  )
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string
  children: ReactNode
  hint?: string
}) {
  return (
    <label className="nx-field">
      <span className="nx-field__label">{label}</span>
      {children}
      {hint && <span className="nx-field__help">{hint}</span>}
    </label>
  )
}

export function Card({
  title,
  actions,
  children,
  className = '',
}: {
  title?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={`nx-card min-w-0 p-0 ${className}`}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-5 py-3.5">
          {title && <h2 className="nx-card__title">{title}</h2>}
          {actions}
        </header>
      )}
      <div className="min-w-0 p-5">{children}</div>
    </section>
  )
}

const badgeStyles = {
  neutral: 'nx-badge--plain bg-surface-sunken text-ink',
  green: 'nx-badge--success',
  blue: 'nx-badge--info',
  amber: 'nx-badge--warning',
  red: 'nx-badge--danger',
} as const

export function Badge({
  tone = 'neutral',
  children,
}: {
  tone?: keyof typeof badgeStyles
  children: ReactNode
}) {
  return (
    <span
      className={`nx-badge ${badgeStyles[tone]}`}
    >
      {children}
    </span>
  )
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 p-10 text-ink-muted">
      <span className="size-5 animate-spin rounded-full border-2 border-line border-t-flow" />
      <span className="font-heading text-sm font-semibold">{label}</span>
    </div>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="nx-empty rounded-md border border-dashed border-line bg-surface-sunken p-8 text-sm text-ink-muted">
      {children}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  const ref = useRef<HTMLParagraphElement>(null)
  useEffect(() => {
    if (error) ref.current?.focus()
  }, [error])
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  return (
    <p
      ref={ref}
      role="alert"
      tabIndex={-1}
      className="nx-banner nx-banner--danger"
    >
      {message}
    </p>
  )
}

export function Pager({
  count,
  page,
  pageSize,
  onPage,
  noun = 'item',
}: {
  count: number
  page: number
  pageSize: number
  onPage: (page: number) => void
  noun?: string
}) {
  // A list that silently shows its first page reads as a complete list, which
  // on a records request is how an inadequate search gets certified as done.
  // The total is stated even when there is only one page, so "12 files" is a
  // fact the reviewer read rather than a count they assumed.
  const pageCount = Math.max(1, Math.ceil(count / pageSize))
  const first = count === 0 ? 0 : (page - 1) * pageSize + 1
  const last = Math.min(page * pageSize, count)
  const plural = count === 1 ? noun : `${noun}s`
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
      <p className="text-sm text-ink" role="status">
        {count === 0
          ? `No ${plural}`
          : `Showing ${first}–${last} of ${count} ${plural}`}
      </p>
      {pageCount > 1 && (
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => onPage(page - 1)}
            disabled={page <= 1}
          >
            Previous
          </Button>
          <span className="font-heading text-sm font-semibold text-ink">
            Page {page} of {pageCount}
          </span>
          <Button
            variant="secondary"
            onClick={() => onPage(page + 1)}
            disabled={page >= pageCount}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  )
}
