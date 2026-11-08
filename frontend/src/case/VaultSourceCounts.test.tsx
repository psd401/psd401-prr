import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { VaultSourceCount } from '../vaultSources'
import { VaultSourceCounts } from './VaultSourceCounts'

const rows: VaultSourceCount[] = [
  { source: 'mail', label: 'Gmail', state: 'counted', count: 1842, reason: '' },
  { source: 'drive', label: 'Drive', state: 'counting', count: null, reason: '' },
  {
    source: 'chat',
    label: 'Chat',
    state: 'unavailable',
    count: null,
    reason: 'Staff organizational unit not set.',
  },
]

describe('VaultSourceCounts', () => {
  it('shows a row per source', () => {
    render(<VaultSourceCounts rows={rows} />)
    expect(screen.getByText('Gmail')).toBeInTheDocument()
    expect(screen.getByText('1,842 messages')).toBeInTheDocument()
    expect(screen.getByText('Counting…')).toBeInTheDocument()
    expect(
      screen.getByText('Not available: Staff organizational unit not set.'),
    ).toBeInTheDocument()
  })

  it('renders nothing without rows', () => {
    const { container } = render(<VaultSourceCounts rows={[]} />)
    expect(container).toBeEmptyDOMElement()
    const none = render(<VaultSourceCounts rows={undefined} />)
    expect(none.container).toBeEmptyDOMElement()
  })
})
