import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

export function SignIn({ hasUsers }: { hasUsers: boolean }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const client = useQueryClient()
  const signIn = useMutation({
    mutationFn: () => api('/auth/login', { method: 'POST', body: { email, password } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['me'] }),
  })

  return (
    <div className="center">
      <form className="card signin" onSubmit={(event) => { event.preventDefault(); signIn.mutate() }}>
        <h1><img src="/favicon.svg" alt="" />bauta Studio</h1>
        {!hasUsers ? (
          <div className="banner info" role="status">
            <div>No accounts yet. On the server, add the first admin:<br /><code>bauta-studio create-user --email you@example.com</code></div>
          </div>
        ) : null}
        <label className="field">Email
          <input type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} />
        </label>
        <label className="field">Password
          <input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        {signIn.error && <p className="error" role="alert">{signIn.error.message}</p>}
        <button type="submit" className="btn primary" disabled={signIn.isPending}>{signIn.isPending ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </div>
  )
}
