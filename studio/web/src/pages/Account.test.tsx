// Tests of the Account page: changing a password, and two-factor sign-in.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { User } from '../api'

const user: User = { id: 1, email: 'ann@example.com', name: 'Ann', role: 'viewer', active: true, createdAt: null, lastLoginAt: null,
  sso: false, passwordSet: true, twoFactor: false }
let signedIn = user
vi.mock('../App', () => ({ useUser: () => signedIn }))
const { Account } = await import('./Account')

/** Renders the Account page for `who`. */
function show(who: User) {
  signedIn = who
  render(<QueryClientProvider client={new QueryClient()}><Account /></QueryClientProvider>)
}

/** Types the three password fields. */
function fill(current: string, next: string, again: string) {
  fireEvent.change(screen.getByLabelText('Current password'), { target: { value: current } })
  fireEvent.change(screen.getByLabelText('New password, at least 12 characters'), { target: { value: next } })
  fireEvent.change(screen.getByLabelText('New password again'), { target: { value: again } })
}

afterEach(cleanup)

describe('your account', () => {
  it('changes the password with the current one, and says so', async () => {
    const sent: unknown[] = []
    vi.stubGlobal('fetch', vi.fn(async (_url: URL, init: RequestInit) => { sent.push(JSON.parse(String(init.body))); return new Response('{}', { status: 200 }) }))
    show(user)
    fill('my old password', 'a brand new passphrase', 'a brand new passphrase')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))

    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('signed out everywhere else'))
    expect(sent).toEqual([{ current: 'my old password', new: 'a brand new passphrase' }])
  })

  it('says when the new passwords differ, and sends nothing', () => {
    const fetching = vi.fn()
    vi.stubGlobal('fetch', fetching)
    show(user)
    fill('my old password', 'a brand new passphrase', 'a brand new passphrass')

    expect(screen.getByRole('alert').textContent).toBe('The new passwords differ.')
    expect((screen.getByRole('button', { name: 'Change password' }) as HTMLButtonElement).disabled).toBe(true)
    expect(fetching).not.toHaveBeenCalled()
  })

  it('shows what the server refused it for', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'your current password is wrong' }), { status: 403 })))
    show(user)
    fill('not my password', 'a brand new passphrase', 'a brand new passphrase')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('your current password is wrong'))
  })

  it('offers no password for someone who signs in only through the identity provider', () => {
    show({ ...user, passwordSet: false, sso: true })

    expect(screen.queryByLabelText('Current password')).toBeNull()
  })
})
