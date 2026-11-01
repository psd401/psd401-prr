// ABOUTME: Responsive, keyboard-operable application shell with role-scoped navigation.
// ABOUTME: Navigation counts are lightweight metadata and never replace API authorization.
import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { appAsset } from '../appAssets'
import { useLogout, type StaffUser } from '../api/auth'
import { PRIMARY_NAV, visibleNav, type NavItem } from '../nav'
import { QueueCountStatus } from '../queueCounts'
import { useQueueCounts } from '../useQueueCounts'
import { itemCountLabel, type WorkItemCounts } from '../work'

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
 * The badge and what it counts. The Inbox badge deliberately counts different
 * things by role -- an officer triages messages needing attention, a liaison
 * has no cross-case message queue at all and works from personal assignments,
 * and everyone else only sees delivery exceptions -- so it must also SAY
 * which, or three people looking at the same number read it three ways.
 */
function navCount(
  item: NavItem,
  counts: WorkItemCounts | undefined,
  user: StaffUser,
): { value: number; description: string } | null {
  if (!item.countKey || !counts) return null
  if (item.countKey === 'review') {
    return { value: counts.review, description: 'awaiting review' }
  }
  if (user.roles.includes('records_officer')) {
    return { value: counts.inbox_needs_attention, description: 'needing attention' }
  }
  if (user.roles.includes('liaison')) {
    return { value: counts.personal, description: 'assigned to you' }
  }
  return { value: counts.inbox_delivery_exceptions, description: 'delivery exceptions' }
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
    <div className="min-h-screen bg-seafoam lg:flex">
      <a
        href="#main-content"
        className="sr-only z-[70] rounded-md bg-white px-4 py-2 font-heading font-semibold text-pacific focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:outline-2 focus:outline-offset-2 focus:outline-whulge"
      >
        Skip to main content
      </a>

      <div className="flex items-center justify-between bg-pacific px-4 py-3 text-skylight lg:hidden">
        <img
          src={appAsset('brand/psd_logo-white-horizontal.png')}
          alt="Peninsula School District"
          className="w-44"
        />
        <button
          ref={menuTrigger}
          type="button"
          aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
          aria-expanded={menuOpen}
          aria-controls="primary-navigation-drawer"
          onClick={() => (menuOpen ? closeMenu(true) : setMenuOpen(true))}
          className="rounded-lg border border-skylight/30 px-3 py-2 font-heading text-sm font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-skylight"
        >
          Menu
        </button>
      </div>

      {menuOpen && !desktop && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={() => closeMenu(true)}
          className="fixed inset-0 z-40 bg-pacific/55 lg:hidden"
        />
      )}

      <aside
        id="primary-navigation-drawer"
        aria-hidden={drawerVisible ? undefined : true}
        inert={!drawerVisible}
        className={`fixed inset-y-0 left-0 z-50 flex w-72 shrink-0 flex-col bg-pacific text-skylight shadow-[4px_0_24px_rgba(37,66,76,0.18)] transition-transform lg:sticky lg:top-0 lg:h-screen lg:w-64 lg:translate-x-0 ${
          menuOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="border-b border-skylight/10 px-5 py-6">
          <img
            src={appAsset('brand/psd_logo-white-horizontal.png')}
            alt="Peninsula School District"
            className="w-full max-w-[190px]"
          />
          <p className="mt-4 font-heading text-[11px] font-semibold uppercase tracking-[0.2em] text-skylight/80">
            Public Records
          </p>
        </div>
        <nav aria-label="Primary navigation" className="flex-1 overflow-y-auto px-3 py-5">
          {nav.map((item) => {
            const count = navCount(item, counts.data, user)
            return (
              <NavLink
                key={item.path}
                to={item.path}
                onClick={closeMenuForNavigation}
                className={({ isActive }) =>
                  `mb-1 flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 font-heading text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-skylight ${
                    isActive
                      ? 'bg-skylight text-pacific shadow-sm'
                      : 'text-skylight/80 hover:bg-skylight/10 hover:text-skylight'
                  }`
                }
              >
                <span>{item.label}</span>
                {count !== null && (
                  <span
                    title={`${count.value} ${count.description}`}
                    aria-label={`${itemCountLabel(count.value, item.label)}, ${count.description}`}
                    className="min-w-7 rounded-full bg-white/15 px-2 py-0.5 text-center text-xs tabular-nums"
                  >
                    <span aria-hidden="true">{count.value}</span>
                  </span>
                )}
              </NavLink>
            )
          })}
        </nav>
        <div className="border-t border-skylight/10 px-5 py-4 text-sm">
          <p className="truncate font-heading font-semibold">{user.username}</p>
          <button
            type="button"
            onClick={() => logout.mutate()}
            className="mt-1 rounded text-xs text-skylight/80 underline-offset-2 hover:text-skylight hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-skylight"
          >
            Sign out
          </button>
        </div>
      </aside>

      <main id="main-content" tabIndex={-1} className="min-w-0 flex-1 p-5 outline-none sm:p-7 lg:p-10">
        <div className="mx-auto max-w-[1500px]">
          <Outlet context={counts.data} />
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
    <header className="mb-7 flex flex-wrap items-end justify-between gap-3 border-b border-driftwood/60 pb-5">
      <div>
        <h1 className="text-3xl font-bold leading-tight tracking-tight text-pacific lg:text-4xl">{title}</h1>
        {subtitle && (
          <p className="mt-1.5 font-editorial text-base text-pacific/80">{subtitle}</p>
        )}
      </div>
      {actions}
    </header>
  )
}
