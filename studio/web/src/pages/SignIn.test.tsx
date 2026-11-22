// Tests of the sign-in page: passwords, single sign-on, the code step and what
// a refusal says.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SignInOptions } from '../api'
import { SignIn } from './SignIn'

/** Renders the sign-in page with `options`. */
function show(options: Partial<SignInOptions> = {}) {
  const shown: SignInOptions = { hasUsers: true, sso: null, passwordLogin: 'all', ...options }
  render(<QueryClientProvider client={new QueryClient()}><SignIn options={shown} /></QueryClientProvider>)
}

/** Types an email and a wrong password, and submits them. */
function submit() {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ann@example.com' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong password!' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
}

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

describe('signing in', () => {
  it('says how to make the first account when there is none', () => {
    show({ hasUsers: false })
    expect(screen.getByRole('status').textContent).toContain('bauta studio create-user')
  })

  it('shows what the server refused it for', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'wrong email or password' }), { status: 401 })))
    show()
    submit()
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('wrong email or password'))
  })

  it('shows being turned away for too many attempts', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'too many failed attempts; try again in a few minutes' }), { status: 429 })))
    show()
    submit()
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('too many failed attempts'))
  })

  it('offers single sign-on as a link to start it, beside the password form', () => {
    show({ sso: { label: 'Sign in with Okta' } })
    expect(screen.getByRole('link', { name: 'Sign in with Okta' }).getAttribute('href')).toBe('/api/auth/sso/start')
    expect(screen.getByLabelText('Password')).toBeTruthy()
  })

  it('shows no password form when passwords are off', () => {
    show({ sso: { label: 'Sign in with SSO' }, passwordLogin: 'off' })
    expect(screen.queryByLabelText('Password')).toBeNull()
  })

  it('says why a single sign-on was refused, once, and nothing it was not sent by the server', () => {
    window.history.replaceState(null, '', '/?sso_error=no_account')
    show({ sso: { label: 'Sign in with SSO' } })
    expect(screen.getByRole('alert').textContent).toContain('don\u2019t have a Studio account')
    expect(window.location.search).toBe('')
    cleanup()

    window.history.replaceState(null, '', '/?sso_error=%3Cscript%3E')
    show({ sso: { label: 'Sign in with SSO' } })
    expect(screen.getByRole('alert').textContent).toContain('Something went wrong')
  })

  it('asks for a code after a password that needs one, then signs in with it', async () => {
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: URL) => {
      requests.push(url.pathname)
      return new Response(JSON.stringify(url.pathname === '/api/auth/login' ? { twoFactorStep: 'code' } : { id: 1 }), { status: 200 })
    }))
    show()
    submit()
    fireEvent.change(await screen.findByLabelText('Code from your authenticator app'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(requests).toContain('/api/auth/2fa/verify'))

    fireEvent.click(screen.getByRole('button', { name: 'Lost your app? Use a recovery code' }))
    expect(screen.getByLabelText('One of your recovery codes')).toBeTruthy()
  })

  it('has someone whose role needs it set an app up, then shows their recovery codes once', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: URL) => {
      const answers: Record<string, unknown> = {
        '/api/auth/login': { twoFactorStep: 'enrol' },
        '/api/auth/2fa/setup': { secret: 'JBSWY3DPEHPK3PXP', uri: 'otpauth://totp/x', qr: 'data:image/svg+xml,x' },
        '/api/auth/2fa/confirm': { recoveryCodes: ['AAAAA-BBBBB', 'CCCCC-DDDDD'] },
      }
      return new Response(JSON.stringify(answers[url.pathname]), { status: 200 })
    }))
    show()
    submit()
    expect((await screen.findByAltText('QR code to set up your authenticator app')).getAttribute('src')).toBe('data:image/svg+xml,x')
    fireEvent.click(screen.getByRole('button', { name: 'Can\u2019t scan it? Type a key instead' }))
    expect(screen.getByText('JBSW Y3DP EHPK 3PXP')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Then the code the app shows'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))

    const done = await screen.findByRole('button', { name: 'Continue to Studio' })
    expect(screen.getByRole('list', { name: 'Recovery codes' }).textContent).toContain('AAAAA-BBBBB')
    expect((done as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByLabelText('I\u2019ve saved them'))
    expect((done as HTMLButtonElement).disabled).toBe(false)
  })
})
