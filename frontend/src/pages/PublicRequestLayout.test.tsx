// ABOUTME: Verifies the anonymous request shell uses deploy-safe brand assets.
// ABOUTME: Prevents root-relative logo paths from breaking under /static/app/.
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const { appAsset } = vi.hoisted(() => ({
  appAsset: vi.fn((path: string) => `/static/app/${path}`),
}))

vi.mock('../appAssets', () => ({ appAsset }))

import { PublicRequestLayout } from './PublicRequestLayout'

describe('PublicRequestLayout', () => {
  it('resolves the district logo from the configured Vite base path', () => {
    render(<PublicRequestLayout>Request form</PublicRequestLayout>)

    expect(appAsset).toHaveBeenCalledWith('brand/psd_logo-2color-emblem.png')
    expect(screen.getByRole('img', { name: 'Peninsula School District' })).toHaveAttribute(
      'src',
      '/static/app/brand/psd_logo-2color-emblem.png',
    )
  })
})
