// ABOUTME: Accessible PSD-branded shell shared by anonymous public pages.
// ABOUTME: It uses only bundled assets and keeps the staff workspace separate.
import type { ReactNode } from 'react'

import { appAsset } from '../appAssets'

export function PublicRequestLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col bg-seafoam text-pacific">
      <a
        href="#public-main"
        className="sr-only z-50 rounded-md bg-white px-4 py-2 font-semibold text-pacific focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:outline-2 focus:outline-offset-2 focus:outline-whulge"
      >
        Skip to request content
      </a>
      <header className="border-b border-driftwood/70 bg-white">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-4 sm:px-6">
          <img
            src={appAsset('brand/psd_logo-2color-emblem.png')}
            alt="Peninsula School District"
            className="size-12 shrink-0"
          />
          <div>
            <p className="font-heading text-sm font-semibold uppercase tracking-[0.08em] text-cedar">
              Peninsula School District
            </p>
            <p className="font-editorial text-lg text-pacific">Public Records</p>
          </div>
        </div>
      </header>
      <main id="public-main" className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6 sm:py-12">
        {children}
      </main>
      <footer className="border-t border-driftwood/70 bg-white">
        <div className="mx-auto max-w-5xl px-4 py-6 text-sm text-pacific/80 sm:px-6">
          Need help? Email{' '}
          <a
            href="mailto:publicrecords@psd401.net"
            className="font-semibold text-whulge underline decoration-ocean/60 underline-offset-2 focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge"
          >
            publicrecords@psd401.net
          </a>
          .
        </div>
      </footer>
    </div>
  )
}
