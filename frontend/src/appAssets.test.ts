// ABOUTME: Verifies public assets honor Vite's base path in preview and deployment.
// ABOUTME: Keeps district branding visible under every supported serving path.
import { describe, expect, it } from 'vitest'
import { appAsset } from './appAssets'

describe('appAsset', () => {
  it('resolves branding from a root-based local preview', () => {
    expect(appAsset('brand/psd_logo-white-horizontal.png', '/')).toBe(
      '/brand/psd_logo-white-horizontal.png',
    )
  })

  it('resolves branding from the deployed static app base', () => {
    expect(appAsset('/brand/psd_logo-2color-emblem.png', '/static/app/')).toBe(
      '/static/app/brand/psd_logo-2color-emblem.png',
    )
  })
})
