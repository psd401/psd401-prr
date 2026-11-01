// ABOUTME: Small PSD-branded UI kit — buttons, inputs, cards, badges — shared
// ABOUTME: by every screen so the app reads as one system.
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'
import { useEffect, useRef } from 'react'

const buttonStyles = {
  primary:
    'bg-cedar text-white hover:bg-pacific disabled:opacity-50 disabled:hover:bg-cedar',
  secondary:
    'bg-whulge text-white hover:bg-pacific disabled:opacity-50 disabled:hover:bg-whulge',
  ghost:
    'bg-transparent text-pacific border border-driftwood hover:bg-skylight disabled:opacity-50',
  danger:
    'bg-red-800 text-white hover:bg-red-900 disabled:opacity-50 disabled:hover:bg-red-800',
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
      className={`rounded-full border px-3 py-1.5 font-heading text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge ${
        selected
          ? 'border-cedar bg-cedar text-white'
          : 'border-driftwood bg-white text-pacific hover:bg-skylight'
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
      className={`inline-flex items-center gap-2 rounded-lg px-3.5 py-2 font-heading text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge ${buttonStyles[variant]} ${className}`}
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
      className={`w-full rounded-lg border border-driftwood bg-white px-3 py-2 text-sm text-pacific shadow-sm placeholder:text-pacific/80 focus:border-seaglass focus:outline-2 focus:outline-whulge/40 ${className}`}
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
      className={`w-full rounded-lg border border-driftwood bg-white px-3 py-2 text-sm text-pacific shadow-sm placeholder:text-pacific/80 focus:border-seaglass focus:outline-2 focus:outline-whulge/40 ${className}`}
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
      className={`rounded-lg border border-driftwood bg-white px-3 py-2 text-sm text-pacific shadow-sm focus:border-seaglass focus:outline-2 focus:outline-whulge/40 ${className}`}
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
    <label className="block space-y-1">
      <span className="font-heading text-sm font-semibold text-pacific">{label}</span>
      {children}
      {hint && <span className="block text-xs text-pacific/80">{hint}</span>}
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
      className={`min-w-0 rounded-xl border border-driftwood/60 bg-white shadow-[0_1px_2px_rgba(37,66,76,0.06),0_8px_24px_rgba(37,66,76,0.04)] ${className}`}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between border-b border-driftwood/40 px-5 py-3.5">
          {title && <h2 className="text-base font-semibold text-pacific">{title}</h2>}
          {actions}
        </header>
      )}
      <div className="min-w-0 p-5">{children}</div>
    </section>
  )
}

const badgeStyles = {
  neutral: 'bg-seafoam text-pacific border-driftwood',
  green: 'bg-seaglass/15 text-cedar border-seaglass/40',
  blue: 'bg-ocean/15 text-whulge border-ocean/40',
  amber: 'bg-amber-100 text-amber-900 border-amber-300',
  red: 'bg-red-100 text-red-900 border-red-300',
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
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 font-heading text-xs font-semibold uppercase tracking-[0.05em] ${badgeStyles[tone]}`}
    >
      {children}
    </span>
  )
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 p-10 text-pacific/80">
      <span className="size-5 animate-spin rounded-full border-2 border-driftwood border-t-seaglass" />
      <span className="font-heading text-sm font-semibold">{label}</span>
    </div>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-driftwood bg-skylight/60 p-8 text-center text-sm text-pacific/80">
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
      className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900"
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
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-driftwood pt-3">
      <p className="text-sm text-pacific" role="status">
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
          <span className="font-heading text-sm font-semibold text-pacific">
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
