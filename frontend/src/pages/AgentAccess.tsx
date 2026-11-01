// ABOUTME: Staff discovery and secure personal-token lifecycle for agent access.
// ABOUTME: Guardrails come from the live capability catalog, never UI policy.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { ApiError, api } from '../api/client'
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
} from '../ui'

type ExecutionPolicy = 'direct' | 'human_confirmation' | 'human_only'

interface PersonalAccessToken {
  id: number
  name: string
  prefix: string
  created_at: string
  expires_at: string
  last_used_at: string | null
  revoked_at: string | null
}

interface PersonalAccessTokenSecret extends PersonalAccessToken {
  token: string
}

interface Capability {
  id: string
  outcome: string
  execution: ExecutionPolicy
}

interface CapabilityCatalog {
  policy: Record<ExecutionPolicy, string>
  capabilities: Capability[]
}

interface TokenConfirmation {
  action: 'rotate' | 'revoke'
  token: PersonalAccessToken
}

const policyOrder: ExecutionPolicy[] = [
  'direct',
  'human_confirmation',
  'human_only',
]

const policyLabels: Record<ExecutionPolicy, string> = {
  direct: 'Direct',
  human_confirmation: 'Human confirmation',
  human_only: 'Human only',
}

function listTokens() {
  return api<PersonalAccessToken[]>('/api/staff/auth/api-tokens/')
}

function listCapabilities() {
  return api<CapabilityCatalog>('/api/staff/capabilities/')
}

function formatDate(value: string | null) {
  if (!value) return 'Never'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

function tokenStatus(token: PersonalAccessToken) {
  if (token.revoked_at) {
    return { label: 'Revoked', tone: 'red' as const }
  }
  const expiresAt = new Date(token.expires_at).getTime()
  const remaining = expiresAt - Date.now()
  if (remaining <= 0) {
    return { label: 'Expired', tone: 'red' as const }
  }
  if (remaining <= 7 * 24 * 60 * 60 * 1000) {
    return { label: 'Expires soon', tone: 'amber' as const }
  }
  return { label: 'Active', tone: 'green' as const }
}

function fieldError(error: unknown, field: string) {
  if (!(error instanceof ApiError) || !error.data || typeof error.data !== 'object') {
    return null
  }
  const value = (error.data as Record<string, unknown>)[field]
  if (Array.isArray(value)) return value.map(String).join(' ')
  return typeof value === 'string' ? value : null
}

function CopyBlock({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(value)
    setCopied(true)
  }

  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-3">
        <h3 className="font-heading text-sm font-bold text-pacific">{label}</h3>
        <Button type="button" variant="ghost" onClick={copy}>
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <pre className="overflow-x-auto rounded-md bg-pacific p-3 text-xs leading-relaxed text-skylight">
        <code>{value}</code>
      </pre>
    </div>
  )
}

export function AgentAccess() {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [lifetimeDays, setLifetimeDays] = useState('30')
  const [shownSecret, setShownSecret] = useState<PersonalAccessTokenSecret | null>(null)
  const [confirmation, setConfirmation] = useState<TokenConfirmation | null>(null)

  const tokens = useQuery({
    queryKey: ['personal-access-tokens'],
    queryFn: listTokens,
  })
  const catalog = useQuery({
    queryKey: ['agent-capabilities'],
    queryFn: listCapabilities,
  })
  const create = useMutation({
    mutationFn: () => api<PersonalAccessTokenSecret>('/api/staff/auth/api-tokens/', {
      method: 'POST',
      body: {
        name,
        lifetime_days: Number(lifetimeDays),
      },
    }),
    onSuccess: (created) => {
      setShownSecret(created)
      setName('')
      setLifetimeDays('30')
      queryClient.invalidateQueries({ queryKey: ['personal-access-tokens'] })
    },
  })
  const rotate = useMutation({
    mutationFn: (token: PersonalAccessToken) =>
      api<PersonalAccessTokenSecret>(`/api/staff/auth/api-tokens/${token.id}/rotate`, {
        method: 'POST',
      }),
    onSuccess: (created) => {
      setShownSecret(created)
      setConfirmation(null)
      queryClient.invalidateQueries({ queryKey: ['personal-access-tokens'] })
    },
  })
  const revoke = useMutation({
    mutationFn: (token: PersonalAccessToken) =>
      api<void>(`/api/staff/auth/api-tokens/${token.id}`, { method: 'DELETE' }),
    onSuccess: () => {
      setConfirmation(null)
      queryClient.invalidateQueries({ queryKey: ['personal-access-tokens'] })
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  const confirmationPending = rotate.isPending || revoke.isPending

  function closeConfirmation() {
    if (!confirmationPending) setConfirmation(null)
  }

  if (tokens.isPending || catalog.isPending) {
    return <Spinner label="Loading agent access…" />
  }
  if (tokens.isError) return <ErrorNote error={tokens.error} />
  if (catalog.isError) return <ErrorNote error={catalog.error} />

  const origin = window.location.origin
  const hostedCode = [
    'claude mcp add --transport http prr-staff',
    `  ${origin}/mcp`,
    '  --header "Authorization: Token YOUR_PERSONAL_TOKEN"',
  ].join(' \\\n')
  const desktopCode = JSON.stringify({
    mcpServers: {
      'prr-staff': {
        command: 'uv',
        args: ['run', '/absolute/path/to/integrations/mcp/server.py'],
        env: {
          PRR_BASE_URL: origin,
          PRR_API_TOKEN: 'YOUR_PERSONAL_TOKEN',
        },
      },
    },
  }, null, 2)
  const stdioCode = [
    'claude mcp add --transport stdio',
    `  --env PRR_BASE_URL=${origin}`,
    '  --env PRR_API_TOKEN=YOUR_PERSONAL_TOKEN',
    '  prr-staff',
    '  -- uv run /absolute/path/to/integrations/mcp/server.py',
  ].join(' \\\n')
  const curlCode = [
    `curl ${origin}/api/staff/requests/`,
    '  --header "Authorization: Token YOUR_PERSONAL_TOKEN"',
  ].join(' \\\n')
  const lifetimeError = fieldError(create.error, 'lifetime_days')
  const nameError = fieldError(create.error, 'name')
  const createError = fieldError(create.error, 'non_field_errors')

  return (
    <>
      <PageHeader
        title="Agent access"
        subtitle="Create attributed credentials, connect an LLM, and inspect the live execution guardrails."
      />

      <div className="space-y-6">
        {shownSecret && (
          <section
            aria-live="polite"
            className="rounded-lg border-2 border-amber-400 bg-amber-50 p-4"
          >
            <h2 className="text-lg text-pacific">Save this token now</h2>
            <p className="mt-1 text-sm text-pacific/75">
              This is the only response that contains the credential. It cannot be
              recovered later.
            </p>
            <div className="mt-3 rounded-md bg-white p-3 font-mono text-sm text-pacific shadow-inner">
              {shownSecret.token}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                type="button"
                onClick={() => navigator.clipboard.writeText(shownSecret.token)}
              >
                Copy token
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => setShownSecret(null)}
              >
                I saved this token
              </Button>
            </div>
          </section>
        )}

        <Card title="Personal access tokens">
          <p className="mb-4 text-sm text-pacific/80">
            Each token carries your current staff roles. Removing your role membership
            removes the token&apos;s effective authority. Tokens cannot perform
            human-only legal actions: approving or queueing correspondence, denying
            a request, excluding a camera, or applying an estimate revision remains
            an accountable staff action in this workspace.
          </p>

          <form
            onSubmit={submit}
            className="mb-5 grid gap-3 rounded-md bg-skylight p-4 md:grid-cols-[minmax(12rem,1fr)_10rem_auto]"
          >
            <Field label="Token name">
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Claude Desktop"
                maxLength={100}
                required
              />
              {nameError && <span role="alert" className="text-xs text-red-800">{nameError}</span>}
            </Field>
            <Field label="Lifetime (days)">
              <Input
                type="number"
                min={1}
                value={lifetimeDays}
                onChange={(event) => setLifetimeDays(event.target.value)}
                required
              />
              {lifetimeError && (
                <span role="alert" className="text-xs text-red-800">{lifetimeError}</span>
              )}
            </Field>
            <div className="self-end">
              <Button type="submit" disabled={create.isPending}>
                Create token
              </Button>
            </div>
            {createError && (
              <p role="alert" className="text-sm text-red-800 md:col-span-3">
                {createError}
              </p>
            )}
            {create.isError && !nameError && !lifetimeError && !createError && (
              <div className="md:col-span-3"><ErrorNote error={create.error} /></div>
            )}
          </form>

          {tokens.data.length === 0 ? (
            <EmptyState>No personal access tokens yet.</EmptyState>
          ) : (
            <Table
              columns={[
                { header: 'Name', cell: (token: PersonalAccessToken) => token.name },
                { header: 'Prefix', cell: (token: PersonalAccessToken) => (
                  <code>{token.prefix}</code>
                ) },
                { header: 'Created', cell: (token: PersonalAccessToken) => formatDate(token.created_at) },
                { header: 'Expires', cell: (token: PersonalAccessToken) => formatDate(token.expires_at) },
                { header: 'Last used', cell: (token: PersonalAccessToken) => formatDate(token.last_used_at) },
                { header: 'Status', cell: (token: PersonalAccessToken) => {
                  const status = tokenStatus(token)
                  return <Badge tone={status.tone}>{status.label}</Badge>
                } },
                { header: 'Actions', cell: (token: PersonalAccessToken) => token.revoked_at ? null : (
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant="ghost"
                      disabled={rotate.isPending}
                      aria-label={`Rotate ${token.name}`}
                      onClick={() => {
                        rotate.reset()
                        setConfirmation({ action: 'rotate', token })
                      }}
                    >
                      Rotate
                    </Button>
                    <Button
                      type="button"
                      variant="danger"
                      disabled={revoke.isPending}
                      aria-label={`Revoke ${token.name}`}
                      onClick={() => {
                        revoke.reset()
                        setConfirmation({ action: 'revoke', token })
                      }}
                    >
                      Revoke
                    </Button>
                  </div>
                ) },
              ]}
              rows={tokens.data}
              rowKey={(token) => token.id}
            />
          )}
        </Card>

        <Modal
          open={confirmation !== null}
          title={confirmation?.action === 'rotate'
            ? 'Rotate personal access token'
            : 'Revoke personal access token'}
          onClose={closeConfirmation}
          dismissible={!confirmationPending}
        >
          {confirmation && (
            <div className="space-y-4">
              <p className="text-sm text-pacific">
                {confirmation.action === 'rotate'
                  ? `Rotate ${confirmation.token.name}? Its current credential will be revoked immediately.`
                  : `Revoke ${confirmation.token.name}? Clients using this credential will immediately lose access.`}
              </p>
              {confirmation.action === 'rotate' && rotate.isError && (
                <ErrorNote error={rotate.error} />
              )}
              {confirmation.action === 'revoke' && revoke.isError && (
                <ErrorNote error={revoke.error} />
              )}
              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  disabled={confirmationPending}
                  onClick={closeConfirmation}
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  variant="danger"
                  disabled={confirmationPending}
                  onClick={() => {
                    if (confirmation.action === 'rotate') rotate.mutate(confirmation.token)
                    else revoke.mutate(confirmation.token)
                  }}
                >
                  {confirmation.action === 'rotate' ? 'Confirm rotate' : 'Confirm revoke'}
                </Button>
              </div>
            </div>
          )}
        </Modal>

        <Card title="Connect Claude Code or Claude Desktop">
          <div className="space-y-5">
            <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
              The hosted <code>/mcp</code> transport is available for clients that
              can send the personal-token header. Use the hosted Claude Code command
              below. Claude Desktop&apos;s local stdio configuration works from a
              repository checkout; Claude.ai/Desktop remote Connectors require OAuth,
              which static-token v1 intentionally does not implement.
            </div>
            <CopyBlock label="Claude Code — hosted MCP" value={hostedCode} />
            <CopyBlock label="Claude Desktop — local stdio configuration" value={desktopCode} />
            <CopyBlock label="Claude Code — local stdio MCP" value={stdioCode} />
          </div>
        </Card>

        <Card title="REST quick start">
          <div className="mb-4 flex flex-wrap gap-x-5 gap-y-2 text-sm">
            <a className="text-ocean underline" href="/api/docs/">Swagger API docs</a>
            <a className="text-ocean underline" href="/api/redoc/">ReDoc API docs</a>
            <a className="text-ocean underline" href="/api/staff/capabilities/">
              Raw capability catalog
            </a>
          </div>
          <CopyBlock label="Authenticated request" value={curlCode} />
        </Card>

        <section>
          <h2 className="mb-1 text-xl text-pacific">Live agent guardrails</h2>
          <p className="mb-4 text-sm text-pacific/80">
            These groups are rendered from the server capability catalog. The UI
            does not decide which actions an agent may execute.
          </p>
          <div className="grid gap-4 xl:grid-cols-3">
            {policyOrder.map((policy) => {
              const capabilities = catalog.data.capabilities.filter(
                (capability) => capability.execution === policy,
              )
              return (
                <Card key={policy}>
                  <h3 className="text-lg text-pacific">{policyLabels[policy]}</h3>
                  <p className="mt-1 text-sm text-pacific/80">{catalog.data.policy[policy]}</p>
                  <ul className="mt-4 space-y-3">
                    {capabilities.map((capability) => (
                      <li key={capability.id} className="border-t border-seafoam pt-3 first:border-0 first:pt-0">
                        <code className="text-xs text-whulge">{capability.id}</code>
                        <p className="mt-1 text-sm text-pacific">{capability.outcome}</p>
                      </li>
                    ))}
                  </ul>
                </Card>
              )
            })}
          </div>
        </section>
      </div>
    </>
  )
}
