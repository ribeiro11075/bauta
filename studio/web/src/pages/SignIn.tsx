import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, signingIn, type SignInOptions, type User } from '../api'
import { CodeInput, RecoveryCodes, TwoFactorSetup } from '../components/TwoFactor'

// What a refused single sign-on came back saying, by the code the server
// gives; anything else in the address is not shown.
const SSO_ERRORS: Record<string, string> = {
  state: 'That sign-in took too long, or was started in another tab or browser. Try again.',
  provider: 'The identity provider didn\u2019t sign you in.',
  unreachable: 'Studio couldn\u2019t reach the identity provider. Try again shortly; if it keeps happening, tell an admin.',
  token: 'Studio couldn\u2019t verify the identity provider\u2019s answer. Tell an admin: Studio\u2019s log says why.',
  email: 'The identity provider didn\u2019t vouch for your email, so Studio can\u2019t match you to an account. Ask an admin.',
  no_account: 'You don\u2019t have a Studio account. Ask an admin to add one with your email.',
  disabled: 'Your Studio account is deactivated.',
  no_role: 'None of your groups at the identity provider gives a role in Studio. Ask an admin.',
  linked: 'Your Studio account is linked to a different identity at the provider. Ask an admin to unlink it.',
  break_glass: 'This is the break-glass admin account, which signs in with its password, not single sign-on.',
  last_admin: 'You’re Studio’s last admin, and your groups at the identity provider no longer make you one. Ask for the admin group back, or have another admin added first.',
  domain: 'Sign in with your organization’s account, not a personal one.',
  busy: 'Too many sign-ins from here have failed. Try again in a few minutes.',
  unexpected: 'Something went wrong signing you in. Studio\u2019s log says what.',
}

function ssoError(): string | null {
  const code = new URLSearchParams(window.location.search).get('sso_error')
  if (code === null) return null
  // Shown once: a reload or a shared link doesn't show it again.
  window.history.replaceState(null, '', window.location.pathname)
  return SSO_ERRORS[code] ?? SSO_ERRORS.unexpected
}

// What a right password needs next, if anything: a code, or setting an app
// up first, for someone whose role needs two-factor sign-in.
type NextStep = 'code' | 'enrol'

function Card({ children }: { children: ReactNode }) {
  return (
    <div className="center">
      <div className="card signin">
        <h1><img src="/favicon.svg" alt="" />bauta Studio</h1>
        {children}
      </div>
    </div>
  )
}

export function SignIn({ options }: { options: SignInOptions }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [next, setNext] = useState<NextStep | null>(null)
  const [refused] = useState(ssoError)
  const client = useQueryClient()
  const signedIn = () => client.invalidateQueries({ queryKey: ['me'] })
  const signIn = useMutation({
    ...signingIn,
    mutationFn: () => api<User | { twoFactorStep: NextStep }>('/auth/login', { method: 'POST', body: { email, password } }),
    onSuccess: (answer) => { if ('twoFactorStep' in answer) setNext(answer.twoFactorStep); else void signedIn() },
  })
  const passwords = options.passwordLogin !== 'off'

  if (next === 'code') return <Card><SecondFactor onSignedIn={signedIn} onExpired={() => setNext(null)} /></Card>
  if (next === 'enrol') return <Card><Enrol onSignedIn={signedIn} /></Card>

  return (
    <Card>
        {!options.hasUsers ? (
          <div className="banner info" role="status">
            <div>No accounts yet. On the server, add the first admin:<br /><code>bauta studio create-user --email you@example.com</code></div>
          </div>
        ) : null}
        {refused && <p className="error" role="alert">{refused}</p>}
        {options.sso && (
          // A navigation, not a request: the identity provider's pages open in this tab.
          <a className="btn primary" href="/api/auth/sso/start">{options.sso.label}</a>
        )}
        {options.sso && passwords && (
          <p className="small muted" style={{ margin: 0, textAlign: 'center' }}>
            {options.passwordLogin === 'admins' ? 'Admins can also sign in with a password' : 'or sign in with a password'}
          </p>
        )}
        {passwords && (
          <form className="signin" onSubmit={(event) => { event.preventDefault(); signIn.mutate() }}>
            <label className="field">Email
              <input type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} />
            </label>
            <label className="field">Password
              <input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
            </label>
            {signIn.error && <p className="error" role="alert">{signIn.error.message}</p>}
            <button type="submit" className={`btn ${options.sso ? '' : 'primary'}`} disabled={signIn.isPending}>
              {signIn.isPending ? 'Signing in\u2026' : 'Sign in'}
            </button>
          </form>
        )}
    </Card>
  )
}

function SecondFactor({ onSignedIn, onExpired }: { onSignedIn: () => void; onExpired: () => void }) {
  const [code, setCode] = useState('')
  const [recovery, setRecovery] = useState(false)
  const verify = useMutation({
    ...signingIn,
    mutationFn: () => api('/auth/2fa/verify', { method: 'POST', body: { code } }),
    onSuccess: onSignedIn,
  })

  return (
    <form className="stack" onSubmit={(event) => { event.preventDefault(); verify.mutate() }}>
      <h2 style={{ margin: 0 }}>Two-factor sign-in</h2>
      <CodeInput key={String(recovery)} value={code} onChange={setCode} recovery={recovery}
        label={recovery ? 'One of your recovery codes' : 'Code from your authenticator app'} />
      {verify.error && <p className="error" role="alert">{verify.error.message}</p>}
      <button type="submit" className="btn primary" disabled={verify.isPending}>{verify.isPending ? 'Checking…' : 'Sign in'}</button>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <button type="button" className="btn link" onClick={() => { setRecovery(!recovery); setCode('') }}>
          {recovery ? 'Use your app instead' : 'Lost your app? Use a recovery code'}
        </button>
        <button type="button" className="btn link" onClick={onExpired}>Start again</button>
      </div>
    </form>
  )
}

function Enrol({ onSignedIn }: { onSignedIn: () => void }) {
  const [codes, setCodes] = useState<string[] | null>(null)

  return (
    <div className="stack">
      <h2 style={{ margin: 0 }}>Set up two-factor sign-in</h2>
      {codes === null ? (
        <>
          <p className="small" style={{ margin: 0 }}>Your role needs a code from an authenticator app at each sign-in, as well as your password.</p>
          <TwoFactorSetup onConfirmed={setCodes} />
        </>
      ) : <RecoveryCodes codes={codes} onDone={onSignedIn} doneLabel="Continue to Studio" />}
    </div>
  )
}
