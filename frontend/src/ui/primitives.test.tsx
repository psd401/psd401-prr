// ABOUTME: Accessibility contracts for shared status and error primitives.
// ABOUTME: Fatal active-view errors receive focus so keyboard users hear them.
import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'

import { ErrorNote } from './primitives'

it('focuses an error summary when it is mounted', () => {
  render(<ErrorNote error={new Error('The active case tab failed.')} />)

  expect(screen.getByRole('alert')).toHaveFocus()
})
