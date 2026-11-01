// ABOUTME: Regression tests for the exemption taxonomy screen.
// ABOUTME: Read-only reviewers must still see each citation's description.
import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeResponse, page, renderPage } from '../test/pageHarness'
import { Exemptions } from './Exemptions'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Exemptions', () => {
  it('shows exemption descriptions to read-only reviewers', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([
      {
        id: 1,
        citation: '20 U.S.C. § 1232g',
        short_name: 'FERPA',
        description: 'Protects student education records.',
        requires_second_review: true,
        active: true,
      },
    ]))))

    renderPage(<Exemptions roles={['reviewer']} />)

    expect(await screen.findByText('Protects student education records.')).toBeInTheDocument()
  })

  it('explains when no exemptions are configured', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fakeResponse(page([]))))
    renderPage(<Exemptions roles={['reviewer']} />)

    expect(await screen.findByText('No exemptions configured.')).toBeInTheDocument()
  })
})
