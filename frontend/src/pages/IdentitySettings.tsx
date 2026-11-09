// ABOUTME: Application-admin UI for staff-role mappings, named direct grants,
// ABOUTME: and integration keys revealed only in provision/rotate responses.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api, unwrap, type Page } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Modal,
  Spinner,
  Table,
  useToast,
} from '../ui'
import { formatMoment } from '../date'

type Role = 'records_officer' | 'intake_clerk' | 'liaison' | 'reviewer' | 'admin'

interface Mapping {
  id: number
  role: Role
  role_label: string
  google_group: string
  active: boolean
  created_at: string
  updated_at: string
}

interface DirectGrant {
  id: number
  email: string
  role: Role
  role_label: string
  active: boolean
  reason: string
  granted_by_username: string
  created_at: string
  updated_at: string
}

interface IntegrationAccount {
  username: string
  token_created_at: string | null
  scope_valid: boolean
}

interface IntegrationIdentity {
  id: string
  label: string
  permission: string
  exists: boolean
  accounts: IntegrationAccount[]
}

interface IssuedCredential extends IntegrationAccount {
  identity_id: string
  label: string
  token: string
}

interface CredentialAction {
  mode: 'provision' | 'rotate'
  identity: IntegrationIdentity
  username: string
}

const roles: { value: Role; label: string }[] = [
  { value: 'records_officer', label: 'Records Officer' },
  { value: 'intake_clerk', label: 'Intake Clerk' },
  { value: 'liaison', label: 'Department Liaison' },
  { value: 'reviewer', label: 'Reviewer' },
  { value: 'admin', label: 'Application Admin' },
]

const emptyForm = { role: 'liaison' as Role, google_group: '', active: true }

interface PersonForm {
  email: string
  roles: Role[]
  reason: string
}

const emptyPersonForm: PersonForm = { email: '', roles: [], reason: '' }

/** Every direct grant for one address, shown and edited as a single person. */
interface GrantPerson {
  email: string
  grants: DirectGrant[]
}

function normalizeEmail(email: string) {
  return email.trim().toLowerCase()
}

function groupGrantsByPerson(grants: DirectGrant[]): GrantPerson[] {
  const people = new Map<string, GrantPerson>()
  for (const grant of grants) {
    const key = normalizeEmail(grant.email)
    const person = people.get(key) ?? { email: grant.email, grants: [] }
    person.grants.push(grant)
    people.set(key, person)
  }
  return [...people.values()]
}

function activeRoleLabels(person: GrantPerson) {
  return roles
    .filter((role) => person.grants.some((grant) => grant.active && grant.role === role.value))
    .map((role) => role.label)
    .join(', ')
}

function unique(values: string[]) {
  return [...new Set(values)].join('; ')
}

/** Mirrors MIN_GRANT_REASON_LENGTH on the server. */
const MIN_GRANT_REASON = 20

function listMappings() {
  return api<Page<Mapping> | Mapping[]>('/api/staff/google-role-mappings/')
}

function listGrants() {
  return api<Page<DirectGrant> | DirectGrant[]>('/api/staff/direct-role-grants/')
}

function listIntegrationIdentities() {
  return api<IntegrationIdentity[]>('/api/staff/integration-identities/')
}

export function IdentitySettings() {
  const qc = useQueryClient()
  const toast = useToast()
  const [editing, setEditing] = useState<Mapping | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState(emptyForm)
  const [deletingMapping, setDeletingMapping] = useState<Mapping | null>(null)
  const [editingPerson, setEditingPerson] = useState<GrantPerson | null>(null)
  const [creatingGrant, setCreatingGrant] = useState(false)
  const [personForm, setPersonForm] = useState<PersonForm>(emptyPersonForm)
  const [deletingPerson, setDeletingPerson] = useState<GrantPerson | null>(null)
  const [credentialAction, setCredentialAction] = useState<CredentialAction | null>(null)
  const [issuedCredential, setIssuedCredential] = useState<IssuedCredential | null>(null)
  const [issuedCredentialCopied, setIssuedCredentialCopied] = useState(false)

  const list = useQuery({
    queryKey: ['google-role-mappings'],
    queryFn: listMappings,
  })
  const grantList = useQuery({
    queryKey: ['direct-role-grants'],
    queryFn: listGrants,
  })
  const integrationList = useQuery({
    queryKey: ['integration-identities'],
    queryFn: listIntegrationIdentities,
  })
  const save = useMutation({
    mutationFn: () => editing
      ? api<Mapping>(`/api/staff/google-role-mappings/${editing.id}/`, {
          method: 'PATCH', body: form,
        })
      : api<Mapping>('/api/staff/google-role-mappings/', { method: 'POST', body: form }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['google-role-mappings'] })
      toast('success', editing ? 'Role mapping updated.' : 'Role mapping added.')
      close()
    },
  })
  const remove = useMutation({
    mutationFn: (id: number) => api<void>(`/api/staff/google-role-mappings/${id}/`, {
      method: 'DELETE',
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['google-role-mappings'] })
      toast('success', 'Role mapping deleted.')
      setDeletingMapping(null)
      close()
    },
  })
  // One person's roles are saved as individual grant changes through the
  // existing audited endpoints. Additions and restorations go first, so
  // swapping one role for another never passes through a state with less
  // access (or trips the last-admin guard on the way).
  const saveGrant = useMutation({
    mutationFn: async () => {
      const email = normalizeEmail(personForm.email)
      const existing = new Map(
        unwrap(grantList.data ?? [])
          .filter((grant) => normalizeEmail(grant.email) === email)
          .map((grant) => [grant.role, grant]),
      )
      const reason = personForm.reason.trim()
      for (const { value: role } of roles) {
        if (!personForm.roles.includes(role)) continue
        const grant = existing.get(role)
        if (!grant) {
          await api<DirectGrant>('/api/staff/direct-role-grants/', {
            method: 'POST', body: { email, role, reason, active: true },
          })
        } else if (!grant.active) {
          await api<DirectGrant>(`/api/staff/direct-role-grants/${grant.id}/`, {
            method: 'PATCH', body: { active: true, reason },
          })
        } else if (grant.reason !== reason) {
          // The form shows one reason per person; keep every held role on it.
          await api<DirectGrant>(`/api/staff/direct-role-grants/${grant.id}/`, {
            method: 'PATCH', body: { reason },
          })
        }
      }
      for (const { value: role } of roles) {
        const grant = existing.get(role)
        if (grant?.active && !personForm.roles.includes(role)) {
          await api<DirectGrant>(`/api/staff/direct-role-grants/${grant.id}/`, {
            method: 'PATCH', body: { active: false },
          })
        }
      }
    },
    onSuccess: () => {
      toast('success', 'Access saved.')
      closeGrant()
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['direct-role-grants'] })
    },
  })
  const removeGrants = useMutation({
    mutationFn: async (person: GrantPerson) => {
      for (const grant of person.grants) {
        await api<void>(`/api/staff/direct-role-grants/${grant.id}/`, { method: 'DELETE' })
      }
    },
    onSuccess: () => {
      toast('success', 'Direct grants deleted.')
      setDeletingPerson(null)
      closeGrant()
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['direct-role-grants'] })
    },
  })
  const issueCredential = useMutation({
    mutationFn: (request: CredentialAction) => request.mode === 'provision'
      ? api<IssuedCredential>(
          `/api/staff/integration-identities/${request.identity.id}/provision/`,
          { method: 'POST', body: { username: request.username } },
        )
      : api<IssuedCredential>(
          `/api/staff/integration-identities/${request.identity.id}/rotate/`,
          { method: 'POST', body: { username: request.username } },
        ),
    onSuccess: (credential) => {
      setIssuedCredential(credential)
      setIssuedCredentialCopied(false)
      setCredentialAction(null)
      qc.invalidateQueries({ queryKey: ['integration-identities'] })
      toast('success', 'Integration credential issued.')
    },
  })

  function open(row?: Mapping) {
    if (row) {
      setEditing(row)
      setForm({ role: row.role, google_group: row.google_group, active: row.active })
    } else {
      setEditing(null)
      setForm(emptyForm)
      setCreating(true)
    }
  }

  function openGrant(person?: GrantPerson) {
    if (person) {
      const latest = [...person.grants]
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
      setEditingPerson(person)
      setPersonForm({
        email: person.email,
        roles: person.grants.filter((grant) => grant.active).map((grant) => grant.role),
        reason: latest?.reason ?? '',
      })
    } else {
      setEditingPerson(null)
      setPersonForm(emptyPersonForm)
      setCreatingGrant(true)
    }
    saveGrant.reset()
  }

  function closeGrant() {
    setCreatingGrant(false)
    setEditingPerson(null)
    setPersonForm(emptyPersonForm)
    saveGrant.reset()
  }

  function toggleRole(role: Role, checked: boolean) {
    setPersonForm({
      ...personForm,
      roles: checked
        ? [...personForm.roles, role]
        : personForm.roles.filter((held) => held !== role),
    })
  }

  function submitGrant(event: FormEvent) {
    event.preventDefault()
    saveGrant.mutate()
  }

  function close() {
    setEditing(null)
    setCreating(false)
    setDeletingMapping(null)
    save.reset()
    remove.reset()
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate()
  }

  function submitCredential(event: FormEvent) {
    event.preventDefault()
    if (credentialAction) issueCredential.mutate(credentialAction)
  }

  function closeCredentialAction() {
    setCredentialAction(null)
    issueCredential.reset()
  }

  function forgetIssuedCredential() {
    setIssuedCredential(null)
    setIssuedCredentialCopied(false)
    issueCredential.reset()
  }

  if (list.isPending || grantList.isPending || integrationList.isPending) {
    return <Spinner />
  }
  if (list.isError) return <ErrorNote error={list.error} />
  if (grantList.isError) return <ErrorNote error={grantList.error} />
  if (integrationList.isError) return <ErrorNote error={integrationList.error} />
  const rows = unwrap(list.data)
  const grantPeople = groupGrantsByPerson(unwrap(grantList.data))

  return (
    <>
      <PageHeader
        title="Staff identity"
        subtitle="Manage district role mappings and isolated integration credentials."
        actions={(
          <div className="flex flex-wrap gap-2">
            <Button variant="ghost" onClick={() => openGrant()}>Add direct grant</Button>
            <Button onClick={() => open()}>Add mapping</Button>
          </div>
        )}
      />
      <div className="space-y-6">
        <Card title="Integration identities">
          <p className="mb-4 text-sm text-ink-muted">
            Each account must carry exactly one integration scope. Newly issued tokens are
            shown once and cannot be retrieved later.
          </p>
          <div className="space-y-4">
            {integrationList.data.map((identity) => (
              <section
                key={identity.id}
                className="rounded-md border border-line bg-surface-sunken p-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="font-heading font-bold text-ink">{identity.label}</h3>
                    <code className="text-xs text-ink-muted">{identity.permission}</code>
                  </div>
                  {!identity.exists && (
                    <Button
                      aria-label={`Provision ${identity.label}`}
                      onClick={() => setCredentialAction({
                        mode: 'provision',
                        identity,
                        username: '',
                      })}
                    >
                      Provision
                    </Button>
                  )}
                </div>
                {!identity.exists ? (
                  <div className="mt-3"><Badge>Not provisioned</Badge></div>
                ) : (
                  <div className="mt-3 space-y-2">
                    {identity.accounts.map((account) => (
                      <div
                        key={account.username}
                        className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line bg-surface-raised p-3"
                      >
                        <div>
                          <p className="font-mono text-sm text-ink">{account.username}</p>
                          <p className="text-xs text-ink-muted">
                            {account.token_created_at
                              ? `Token created ${formatMoment(account.token_created_at)}`
                              : 'No token issued'}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          <Badge tone={account.scope_valid ? 'green' : 'red'}>
                            {account.scope_valid ? 'Exact scope' : 'Invalid scope'}
                          </Badge>
                          {account.scope_valid && account.token_created_at && (
                            <Button
                              variant="danger"
                              aria-label={`Rotate ${account.username}`}
                              onClick={() => setCredentialAction({
                                mode: 'rotate',
                                identity,
                                username: account.username,
                              })}
                            >
                              Rotate
                            </Button>
                          )}
                          {account.scope_valid && !account.token_created_at && (
                            <Button
                              aria-label={`Provision token for ${account.username}`}
                              onClick={() => setCredentialAction({
                                mode: 'provision',
                                identity,
                                username: account.username,
                              })}
                            >
                              Provision token
                            </Button>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            ))}
          </div>
        </Card>
        <Card title="Google role mappings">
          {rows.length === 0 ? <EmptyState>No Google role mappings configured.</EmptyState> : (
            <Table
              columns={[
                { header: 'Application role', cell: (row: Mapping) => row.role_label },
                { header: 'Google group', cell: (row: Mapping) => row.google_group },
                { header: 'Status', cell: (row: Mapping) => row.active
                  ? <Badge tone="green">Active</Badge> : <Badge>Inactive</Badge> },
              ]}
              rows={rows}
              rowKey={(row) => row.id}
              onRowClick={open}
            />
          )}
        </Card>
        <Card title="Direct role grants">
          <p className="mb-4 text-sm text-ink-muted">
            Group membership is the normal way to hold a role. A direct grant
            names one person who needs the system but belongs to no group that
            can be synced. Grants add to whatever the groups already say and
            never take a role away. Click a person to tick or untick their
            roles; an unticked role is removed immediately.
          </p>
          {grantPeople.length === 0 ? (
            <EmptyState>No direct role grants configured.</EmptyState>
          ) : (
            <Table
              columns={[
                { header: 'Person', cell: (row: GrantPerson) => row.email },
                { header: 'Application roles', cell: (row: GrantPerson) => activeRoleLabels(row)
                  || <Badge>No active roles</Badge> },
                { header: 'Reason', cell: (row: GrantPerson) => unique(row.grants.map((grant) => grant.reason)) },
                { header: 'Granted by', cell: (row: GrantPerson) => unique(
                  row.grants.map((grant) => grant.granted_by_username),
                ) },
              ]}
              rows={grantPeople}
              rowKey={(row) => normalizeEmail(row.email)}
              onRowClick={openGrant}
            />
          )}
        </Card>
      </div>
      <Modal open={creating || editing !== null} title={editing ? 'Edit role mapping' : 'Add role mapping'} onClose={close}>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Application role">
            <select
              className="w-full rounded-md border border-line bg-surface-raised px-3 py-2 text-sm text-ink"
              value={form.role}
              onChange={(event) => setForm({ ...form, role: event.target.value as Role })}
            >
              {roles.map((role) => <option key={role.value} value={role.value}>{role.label}</option>)}
            </select>
          </Field>
          <Field label="Google group" hint="Use the existing full group email; no prefix is assumed.">
            <Input
              type="email"
              value={form.google_group}
              onChange={(event) => setForm({ ...form, google_group: event.target.value })}
              required
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.active}
              onChange={(event) => setForm({ ...form, active: event.target.checked })}
            />
            Active
          </label>
          {save.isError && <ErrorNote error={save.error} />}
          <div className="flex justify-between gap-2">
            <div>{editing && <Button type="button" variant="danger" onClick={() => {
              remove.reset()
              setDeletingMapping(editing)
            }}>Delete</Button>}</div>
            <div className="flex gap-2">
              <Button type="button" variant="secondary" onClick={close}>Cancel</Button>
              <Button type="submit" disabled={save.isPending}>Save mapping</Button>
            </div>
          </div>
        </form>
      </Modal>
      <Modal
        open={creatingGrant || editingPerson !== null}
        title={editingPerson ? `Edit access for ${editingPerson.email}` : 'Add direct grant'}
        onClose={closeGrant}
      >
        <form onSubmit={submitGrant} className="space-y-4">
          {!editingPerson && (
            <Field
              label="Person"
              hint="The exact district Workspace address, not a group."
            >
              <Input
                type="email"
                value={personForm.email}
                onChange={(event) => setPersonForm({ ...personForm, email: event.target.value })}
                required
              />
            </Field>
          )}
          <fieldset className="space-y-2">
            <legend className="mb-1 text-sm font-semibold text-ink">Application roles</legend>
            {roles.map((role) => (
              <label key={role.value} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={personForm.roles.includes(role.value)}
                  onChange={(event) => toggleRole(role.value, event.target.checked)}
                />
                {role.label}
              </label>
            ))}
          </fieldset>
          <Field
            label="Why this person is granted directly"
            hint={`Required — at least ${MIN_GRANT_REASON} characters; read during access reviews.`}
          >
            <Input
              type="text"
              minLength={MIN_GRANT_REASON}
              maxLength={500}
              value={personForm.reason}
              onChange={(event) => setPersonForm({ ...personForm, reason: event.target.value })}
              required
            />
          </Field>
          {saveGrant.isError && <ErrorNote error={saveGrant.error} />}
          <div className="flex justify-between gap-2">
            <div>{editingPerson && <Button type="button" variant="danger" onClick={() => {
              removeGrants.reset()
              setDeletingPerson(editingPerson)
            }}>Delete all grants</Button>}</div>
            <div className="flex gap-2">
              <Button type="button" variant="secondary" onClick={closeGrant}>Cancel</Button>
              <Button
                type="submit"
                disabled={
                  saveGrant.isPending
                  || personForm.reason.trim().length < MIN_GRANT_REASON
                  || (!editingPerson && personForm.roles.length === 0)
                }
              >
                Save access
              </Button>
            </div>
          </div>
        </form>
      </Modal>
      <Modal
        open={deletingPerson !== null}
        title="Delete direct grants"
        onClose={() => setDeletingPerson(null)}
      >
        {deletingPerson && (
          <div className="space-y-4">
            <p className="text-sm text-ink">
              Delete every direct grant for <strong>{deletingPerson.email}</strong>? They
              lose those roles immediately unless a configured group also gives them.
            </p>
            {removeGrants.isError && <ErrorNote error={removeGrants.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => setDeletingPerson(null)}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="danger"
                disabled={removeGrants.isPending}
                onClick={() => removeGrants.mutate(deletingPerson)}
              >
                Delete grants
              </Button>
            </div>
          </div>
        )}
      </Modal>
      <Modal
        open={deletingMapping !== null}
        title="Delete role mapping"
        onClose={() => setDeletingMapping(null)}
      >
        {deletingMapping && (
          <div className="space-y-4">
            <p className="text-sm text-ink">
              Delete the mapping from <strong>{deletingMapping.google_group}</strong> to{' '}
              <strong>{deletingMapping.role_label}</strong>? Staff relying on this group
              will lose that application role after authorization is refreshed.
            </p>
            {remove.isError && <ErrorNote error={remove.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => setDeletingMapping(null)}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(deletingMapping.id)}
              >
                {remove.isPending ? 'Deleting…' : 'Confirm delete'}
              </Button>
            </div>
          </div>
        )}
      </Modal>
      <Modal
        open={credentialAction !== null}
        title={credentialAction?.mode === 'rotate'
          ? 'Rotate integration token'
          : 'Provision integration identity'}
        onClose={closeCredentialAction}
      >
        {credentialAction && (
          <form onSubmit={submitCredential} className="space-y-4">
            {credentialAction.mode === 'provision' ? (
              <Field
                label="Service username"
                hint="Use one unique, noninteractive username for this workflow."
              >
                <Input
                  aria-label="Service username"
                  value={credentialAction.username}
                  onChange={(event) => setCredentialAction({
                    ...credentialAction,
                    username: event.target.value,
                  })}
                  required
                  autoFocus
                />
              </Field>
            ) : (
              <p className="text-sm text-ink">
                Rotating <strong>{credentialAction.username}</strong> immediately revokes
                its current token. Update the integration before its next request.
              </p>
            )}
            {issueCredential.isError && <ErrorNote error={issueCredential.error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={closeCredentialAction}>
                Cancel
              </Button>
              <Button type="submit" disabled={issueCredential.isPending}>
                {credentialAction.mode === 'rotate' ? 'Rotate token' : 'Provision identity'}
              </Button>
            </div>
          </form>
        )}
      </Modal>
      <Modal
        open={issuedCredential !== null}
        title="Store integration token now"
        onClose={() => undefined}
        dismissible={false}
      >
        {issuedCredential && (
          <div className="space-y-4">
            <p className="text-sm text-ink">
              Store this token in the integration credential now. It will not be shown again.
            </p>
            <code className="block break-all rounded-md border border-line bg-surface-raised p-3 text-sm text-ink">
              {issuedCredential.token}
            </code>
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="secondary"
                onClick={async () => {
                  await navigator.clipboard.writeText(issuedCredential.token)
                  setIssuedCredentialCopied(true)
                }}
              >
                {issuedCredentialCopied ? 'Copied' : 'Copy token'}
              </Button>
              <Button onClick={forgetIssuedCredential}>I stored this token</Button>
            </div>
          </div>
        )}
      </Modal>
    </>
  )
}
