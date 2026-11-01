// ABOUTME: Login UI contract: deployed builds expose Google SSO only while
// ABOUTME: local developers retain the password form without cloud access.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { Login } from './Login'

function renderLogin(mode: 'local' | 'google') {
  let root = document.getElementById('root')
  if (!root) {
    root = document.createElement('div')
    root.id = 'root'
    document.body.appendChild(root)
  }
  root.setAttribute('data-auth-mode', mode)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Login />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  document.getElementById('root')?.removeAttribute('data-auth-mode')
  window.history.replaceState({}, '', '/')
})

describe('Login', () => {
  it('shows only Google sign-in in deployed mode', () => {
    renderLogin('google')

    expect(screen.getByRole('link', { name: 'Sign in with Google' })).toHaveAttribute(
      'href',
      '/api/staff/auth/oidc/authenticate/',
    )
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })

  it('explains a denied Google callback without exposing internal details', () => {
    window.history.replaceState({}, '', '/?login=denied')

    renderLogin('google')

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Google sign-in could not authorize this account',
    )
  })

  it('shows the password form only in local developer mode', () => {
    renderLogin('local')

    expect(screen.getByLabelText('Username')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Sign in with Google' })).not.toBeInTheDocument()
  })
})
