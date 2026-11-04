// ABOUTME: The trust footer states scope and side effects; the theme picker switches
// ABOUTME: data-theme and remembers the choice in this browser.
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it } from 'vitest'

import { THEME_STORAGE_KEY } from '../theme'
import { STAFF_TRUST_FACTS, ThemePicker, TrustFooter } from '.'

afterEach(() => {
  window.localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

it('joins the staff trust facts on one nx-trust line', () => {
  render(<TrustFooter />)
  const line = screen.getByText(STAFF_TRUST_FACTS.join(' · '))
  expect(line).toHaveClass('nx-trust')
})

it('takes screen-specific facts', () => {
  render(<TrustFooter facts={['Draft only', 'nothing sent']} />)
  expect(screen.getByText('Draft only · nothing sent')).toBeInTheDocument()
})

it('switches every theme and remembers the choice', async () => {
  const user = userEvent.setup()
  render(<ThemePicker />)
  const picker = screen.getByLabelText('Appearance')
  expect(picker).toHaveValue('system')
  for (const theme of ['nexus', 'commons', 'dark', 'commons-dark', 'high-contrast']) {
    await user.selectOptions(picker, theme)
    expect(document.documentElement.dataset.theme).toBe(theme)
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe(theme)
  }
  await user.selectOptions(picker, 'system')
  expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
})
