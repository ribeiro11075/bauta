import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SignIn } from './SignIn'

function show(hasUsers = true) {
  render(<QueryClientProvider client={new QueryClient()}><SignIn hasUsers={hasUsers} /></QueryClientProvider>)
}

function submit() {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ann@example.com' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong password!' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
}

afterEach(cleanup)

describe('signing in', () => {
  it('says how to make the first account when there is none', () => {
    show(false)
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
})
