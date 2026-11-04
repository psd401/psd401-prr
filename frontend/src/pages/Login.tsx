// ABOUTME: Branded staff sign-in: Google SSO in deployed mode and a local-only
// ABOUTME: Django password form so developers need no cloud credentials.
import { useState, type FormEvent } from 'react'
import { authBootstrap, googleLoginHref, useLogin } from '../api/auth'
import { appAsset } from '../appAssets'
import { Button, ErrorNote, Field, Input } from '../ui'

export function Login() {
  const auth = authBootstrap()
  const googleDenied = new URLSearchParams(window.location.search).get('login') === 'denied'
  const login = useLogin()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  function submit(e: FormEvent) {
    e.preventDefault()
    login.mutate({ username, password })
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-surface-header p-4">
      <div className="w-full max-w-sm rounded-2xl border border-line bg-surface-raised p-8 shadow-(--shadow-overlay)">
        <img
          src={appAsset('brand/psd_logo-2color-emblem.png')}
          alt="Peninsula School District"
          className="mx-auto mb-4 w-28"
        />
        <h1 className="text-center text-3xl font-bold tracking-tight text-ink">Public Records</h1>
        <p className="mb-6 mt-1 text-center font-editorial text-base text-ink-muted">
          Staff workspace sign-in
        </p>
        {auth.mode === 'google' && googleDenied && (
          <p role="alert" className="mb-4 rounded-md bg-danger-soft p-3 text-sm text-danger-ink">
            Google sign-in could not authorize this account. Confirm that it belongs to the
            district domain and is a direct member of a configured PRR role group.
          </p>
        )}
        {auth.mode === 'google' ? (
          <a
            href={googleLoginHref(auth.googleLoginUrl, window.location)}
            className="block w-full rounded-md bg-action px-3.5 py-2 text-center font-heading text-sm font-bold tracking-wide text-on-action transition-colors hover:bg-action-hover"
          >
            Sign in with Google
          </a>
        ) : <form onSubmit={submit} className="space-y-4">
          <Field label="Username">
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>
          {login.isError && <ErrorNote error={login.error} />}
          <Button type="submit" className="w-full justify-center" disabled={login.isPending}>
            {login.isPending ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>}
      </div>
    </main>
  )
}
