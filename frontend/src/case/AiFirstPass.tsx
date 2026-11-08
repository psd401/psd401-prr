// ABOUTME: One line on Review and Redact saying how many of the case's records
// ABOUTME: the AI has read, or that its first pass is off and where to turn it on.
import { Link } from 'react-router'

export interface FirstPassProgress {
  enabled: boolean
  read: number
  total: number
}

const count = new Intl.NumberFormat('en-US')

export function AiFirstPass({ progress }: { progress: FirstPassProgress }) {
  if (progress.total === 0) return null
  if (!progress.enabled) {
    return (
      <p className="text-sm text-ink-muted">
        AI is not reading this case's records, so nothing is suggested until you ask.{' '}
        <Link to="/configuration/settings" className="font-semibold text-accent underline-offset-2 hover:underline">
          Turn it on in Settings
        </Link>
      </p>
    )
  }
  if (progress.read >= progress.total) {
    return (
      <p className="text-sm text-ink-muted">
        AI has read all {count.format(progress.total)} records.
      </p>
    )
  }
  return (
    <div className="space-y-1">
      <p className="text-sm text-ink">
        AI has read {count.format(progress.read)} of {count.format(progress.total)} records.
        <span className="text-ink-muted"> Its suggestions appear as it goes.</span>
      </p>
      <div
        role="progressbar"
        aria-label="AI first pass"
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-valuenow={progress.read}
        className="h-1.5 overflow-hidden rounded-full bg-surface-sunken"
      >
        <div
          className="h-full rounded-full bg-flow"
          style={{ width: `${Math.round((progress.read / progress.total) * 100)}%` }}
        />
      </div>
    </div>
  )
}
