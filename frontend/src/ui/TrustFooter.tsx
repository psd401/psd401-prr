// ABOUTME: Nexus trust footer — one line stating what this screen can see and what
// ABOUTME: it has (and has not) done. Required wherever district data is read or drafted.
import { STAFF_TRUST_FACTS } from './trustFacts'

export function TrustFooter({
  facts = STAFF_TRUST_FACTS,
  className = '',
}: {
  /** Two to four short facts: state, data scope, side effects. */
  facts?: string[]
  className?: string
}) {
  return (
    <footer className={className}>
      <p className="nx-trust">
        <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path
            d="M8 1.5 2.5 3.5v4c0 3.2 2.3 5.9 5.5 7 3.2-1.1 5.5-3.8 5.5-7v-4L8 1.5Z"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinejoin="round"
          />
        </svg>
        {facts.join(' · ')}
      </p>
    </footer>
  )
}
