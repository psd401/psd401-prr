// ABOUTME: Accessible landing cards for Configuration and Oversight resources.
// ABOUTME: Cards use the same role registry as direct-route authorization and primary navigation.
import { Link } from 'react-router'
import type { StaffRole } from '../api/auth'
import { CONFIGURATION_NAV, OVERSIGHT_NAV, visibleNav, type NavItem } from '../nav'
import { PageHeader } from '../layout/AppLayout'
import { EmptyState } from '../ui'

function SectionLinks({ items }: { items: NavItem[] }) {
  if (items.length === 0) return <EmptyState>No resources are available for your current roles.</EmptyState>
  return (
    <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {items.map((item) => (
        <li key={item.path}>
          <Link
            to={item.path}
            className="block h-full rounded-xl border border-line bg-surface-raised p-5 shadow-sm transition hover:border-flow hover:shadow-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            <span className="font-heading text-lg font-bold text-ink">{item.label}</span>
            {item.description && (
              <span className="mt-2 block text-sm leading-relaxed text-ink-muted">{item.description}</span>
            )}
          </Link>
        </li>
      ))}
    </ul>
  )
}

export function ConfigurationIndex({ roles }: { roles: StaffRole[] }) {
  return (
    <>
      <PageHeader title="Configuration" subtitle="Personal access and role-scoped application settings." />
      <SectionLinks items={visibleNav(CONFIGURATION_NAV, roles)} />
    </>
  )
}

export function OversightIndex({ roles }: { roles: StaffRole[] }) {
  return (
    <>
      <PageHeader title="Oversight" subtitle="Compliance reporting and append-only accountability evidence." />
      <SectionLinks items={visibleNav(OVERSIGHT_NAV, roles)} />
    </>
  )
}
