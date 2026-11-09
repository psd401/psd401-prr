// ABOUTME: The AI first-pass line on Review and Redact: how many of the
// ABOUTME: case's records the AI has read, or that the first pass is off.
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { expect, it } from 'vitest'
import { AiFirstPass } from './AiFirstPass'

function show(progress: { enabled: boolean; read: number; total: number }) {
  render(<MemoryRouter><AiFirstPass progress={progress} /></MemoryRouter>)
}

it('counts the records the AI has read so far', () => {
  show({ enabled: true, read: 1200, total: 3647 })
  expect(screen.getByText('AI has read 1,200 of 3,647 records.')).toBeInTheDocument()
  expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '1200')
})

it('says when the AI has read everything', () => {
  show({ enabled: true, read: 12, total: 12 })
  expect(screen.getByText('AI has read all 12 records.')).toBeInTheDocument()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})

it('says where to turn the first pass on when it is off', () => {
  show({ enabled: false, read: 0, total: 40 })
  expect(screen.getByText(/AI is not reading this case's records/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Turn it on in Settings' })).toHaveAttribute(
    'href', '/configuration/settings',
  )
})

it('shows nothing before any record arrives', () => {
  const { container } = render(
    <MemoryRouter><AiFirstPass progress={{ enabled: true, read: 0, total: 0 }} /></MemoryRouter>,
  )
  expect(container).toBeEmptyDOMElement()
})
