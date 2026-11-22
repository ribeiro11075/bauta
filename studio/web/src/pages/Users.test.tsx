// Tests of the Users page: how each person signs in, adding accounts and
// setting passwords.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { User } from '../api'
import { signsInWith, Users, type UsersPage } from './Users'

vi.mock('../App', () => ({ useUser: () => ({ ...person, id: 99, email: 'me@example.com', role: 'admin' }) }))

const person: User = { id: 1, email: 'ann@example.com', name: '', role: 'viewer', active: true, createdAt: null, lastLoginAt: null,
  sso: false, passwordSet: true, twoFactor: false }
const page: UsersPage = { users: [], sso: { roles: 'studio' }, passwordLogin: 'all', requireTwoFactor: 'off' }

afterEach(cleanup)

describe('how someone signs in', () => {
  it('names both ways for someone linked with a password', () => {
    expect(signsInWith({ ...person, sso: true }, page)).toBe('SSO or password')
  })

  it('leaves out a password the policy does not let them use', () => {
    expect(signsInWith({ ...person, sso: true }, { ...page, passwordLogin: 'admins' })).toBe('SSO')
    expect(signsInWith({ ...person, role: 'admin' }, { ...page, passwordLogin: 'admins' })).toBe('password')
  })

  it('says when a password needs a code, now or from the next sign-in', () => {
    expect(signsInWith({ ...person, twoFactor: true }, page)).toBe('password + 2FA')
    expect(signsInWith(person, { ...page, requireTwoFactor: 'all' })).toBe('password, 2FA at next sign-in')
  })

  it('says someone with no way yet will sign in with SSO', () => {
    expect(signsInWith({ ...person, passwordSet: false }, page)).toBe('SSO, once they first sign in')
  })
})

describe('setting someone\'s password', () => {
  it('sends it once both entries agree', async () => {
    // jsdom has no modal dialogs: showModal just opens one.
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) { this.open = true }
    HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) { this.open = false }
    const sent: { url: string; body: unknown }[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: URL, init: RequestInit) => {
      if (init.method === 'PATCH') sent.push({ url: url.pathname, body: JSON.parse(String(init.body)) })
      return new Response(JSON.stringify({ ...page, users: [person] }), { status: 200 })
    }))
    render(<QueryClientProvider client={new QueryClient()}><Users /></QueryClientProvider>)

    fireEvent.click(await screen.findByRole('button', { name: 'Set password' }))
    const dialog = screen.getByRole('dialog', { name: /Set ann@example.com/ })
    fireEvent.change(within(dialog).getByLabelText('New password, at least 12 characters'), { target: { value: 'a long new password' } })
    fireEvent.change(within(dialog).getByLabelText('Again'), { target: { value: 'a long new passwort' } })
    expect(within(dialog).getByRole('alert').textContent).toBe('The passwords differ.')

    fireEvent.change(within(dialog).getByLabelText('Again'), { target: { value: 'a long new password' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Set password' }))
    await waitFor(() => expect(sent).toEqual([{ url: '/api/users/1', body: { password: 'a long new password' } }]))
  })

  it('offers no password where the policy would not let the role use one', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...page, passwordLogin: 'admins', users: [person] }), { status: 200 })))
    render(<QueryClientProvider client={new QueryClient()}><Users /></QueryClientProvider>)

    await screen.findByRole('table')
    expect(screen.queryByRole('button', { name: /password/i })).toBeNull()
  })
})
