// ABOUTME: Responsive, keyboard-operable application shell with role-scoped navigation.
// ABOUTME: Three menu stops; route authorization stays with the API.
import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { appAsset } from '../appAssets'
import { useLogout, type StaffUser } from '../api/auth'
import { PRIMARY_NAV, visibleNav } from '../nav'
import { QueueCountStatus } from '../queueCounts'
import { useQueueCounts } from '../useQueueCounts'
import { ThemePicker, TrustFooter } from '../ui'

function useDesktopLayout() {
  const [desktop, setDesktop] = useState(
    () => window.matchMedia?.('(min-width: 1024px)').matches ?? true,
  )

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined
    const media = window.matchMedia('(min-width: 1024px)')
    const update = () => setDesktop(media.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  return desktop
}

/**
 * The official white logo, except on Commons, whose header is light. There the
 * two-color emblem stands in: logos are never recolored to fit a theme.
 */
function DistrictLogo({ className }: { className: string }) {
  return (
    <>
      <img
        src={appAsset('brand/psd_logo-white-horizontal.png')}
        alt="Peninsula School District"
        className={`${className} commons:hidden`}
      />
      <span className="hidden items-center gap-2 font-editorial text-lg leading-tight commons:flex">
        <img
          src={appAsset('brand/psd_logo-2color-emblem.png')}
          alt=""
          className="size-10"
        />
        Peninsula School District
      </span>
    </>
  )
}

export function AppLayout({ user }: { user: StaffUser }) {
  const logout = useLogout()
  const nav = visibleNav(PRIMARY_NAV, user.roles)
  const counts = useQueueCounts(user.roles)
  const desktop = useDesktopLayout()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuTrigger = useRef<HTMLButtonElement>(null)

  function closeMenu(returnFocus: boolean) {
    setMenuOpen(false)
    if (returnFocus) window.requestAnimationFrame(() => menuTrigger.current?.focus())
  }

  function closeMenuForNavigation() {
    setMenuOpen(false)
    if (!desktop) {
      window.requestAnimationFrame(() => {
        document.getElementById('main-content')?.focus()
      })
    }
  }

  useEffect(() => {
    if (!menuOpen || desktop) return undefined
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') closeMenu(true)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [desktop, menuOpen])

  const drawerVisible = desktop || menuOpen

  return (
    <div className="min-h-screen bg-surface-canvas lg:flex">
      <a
        href="#main-content"
        className="sr-only z-[70] rounded-md bg-surface-raised px-4 py-2 font-heading font-semibold text-ink focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:outline-2 focus:outline-offset-2 focus:outline-focus"
      >
        Skip to main content
      </a>

      <div className="flex items-center justify-between bg-surface-header px-4 py-3 text-on-header lg:hidden">
        <DistrictLogo className="w-44" />
        <button
          ref={menuTrigger}
          type="button"
          aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
          aria-expanded={menuOpen}
          aria-controls="primary-navigation-drawer"
          onClick={() => (menuOpen ? closeMenu(true) : setMenuOpen(true))}
          className="rounded-lg border border-on-header-muted px-3 py-2 font-heading text-sm font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-on-header"
        >
          Menu
        </button>
      </div>

      {menuOpen && !desktop && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={() => closeMenu(true)}
          className="fixed inset-0 z-40 bg-scrim lg:hidden"
        />
      )}

      <aside
        id="primary-navigation-drawer"
        aria-hidden={drawerVisible ? undefined : true}
        inert={!drawerVisible}
        className={`fixed inset-y-0 left-0 z-50 flex w-72 shrink-0 flex-col bg-surface-header text-on-header shadow-(--shadow-pop) transition-transform lg:sticky lg:top-0 lg:h-screen lg:w-64 lg:translate-x-0 ${
          menuOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="border-b border-on-header-muted/20 px-5 py-6">
          <DistrictLogo className="w-full max-w-48" />
          <p className="mt-4 font-heading text-xs font-semibold uppercase tracking-[0.2em] text-on-header-muted">
            Public Records
          </p>
        </div>
        <nav aria-label="Primary navigation" className="flex-1 overflow-y-auto px-3 py-5">
          {nav.map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                onClick={closeMenuForNavigation}
                className={({ isActive }) =>
                  `mb-1 flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 font-heading text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-on-header ${
                    isActive
                      ? 'bg-surface-raised text-ink shadow-sm'
                      : 'text-on-header-muted hover:bg-on-header/10 hover:text-on-header'
                  }`
                }
              >
                <span>{item.label}</span>
              </NavLink>
          ))}
        </nav>
        <div className="border-t border-on-header-muted/20 px-5 py-4 text-sm">
          <p className="truncate font-heading font-semibold">{user.username}</p>
          <button
            type="button"
            onClick={() => logout.mutate()}
            className="mt-1 rounded text-xs text-on-header-muted underline-offset-2 hover:text-on-header hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-on-header"
          >
            Sign out
          </button>
          <ThemePicker className="mt-4 text-on-header-muted" />
        </div>
      </aside>

      <main id="main-content" tabIndex={-1} className="min-w-0 flex-1 p-5 outline-none sm:p-7 lg:p-10">
        <div className="mx-auto max-w-[1500px]">
          <Outlet context={counts.data} />
          {/* Every staff screen reads district records, so every one says so. */}
          <TrustFooter className="mt-10 border-t border-line pt-4" />
        </div>
      </main>
      <QueueCountStatus counts={counts.data} />
    </div>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: string
  actions?: React.ReactNode
}) {
  return (
    <header className="mb-7 flex flex-wrap items-end justify-between gap-3 border-b border-line pb-5">
      <div>
        <h1 className="text-3xl font-bold leading-tight tracking-tight text-ink lg:text-4xl">{title}</h1>
        {subtitle && (
          <p className="mt-1.5 font-editorial text-base text-ink-muted">{subtitle}</p>
        )}
      </div>
      {actions}
    </header>
  )
}
