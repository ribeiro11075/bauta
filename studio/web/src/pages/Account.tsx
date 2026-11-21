import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type User } from '../api'
import { useUser } from '../App'
import { PageHead, Pill } from '../components/ui'
import { CodeInput, RecoveryCodes, TwoFactorSetup } from '../components/TwoFactor'

// Someone's own account: their password and their two-factor sign-in.
export function Account() {
  const user = useUser()

  return (
    <>
      <PageHead title="Your account" sub={<>{user.email}, {user.role}</>} />
      {user.passwordSet && <ChangePassword />}
      <section className="card stack" style={{ maxWidth: 560 }} aria-labelledby="two-factor-title">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h2 id="two-factor-title" style={{ margin: 0 }}>Two-factor sign-in</h2>
          {user.passwordSet && (user.twoFactor ? <Pill kind="ok">On</Pill> : <Pill kind="idle">Off</Pill>)}
        </div>
        {!user.passwordSet ? (
          <p className="muted" style={{ margin: 0 }}>You sign in through your identity provider, which asks for its own second factor.</p>
        ) : user.twoFactor ? <TwoFactorOn /> : <TwoFactorOff />}
      </section>
    </>
  )
}

function ChangePassword() {
  const empty = { current: '', new: '', again: '' }
  const [form, setForm] = useState(empty)
  const [changed, setChanged] = useState(false)
  const change = useMutation({
    mutationFn: () => api<User>('/auth/password', { method: 'POST', body: { current: form.current, new: form.new } }),
    onSuccess: () => { setForm(empty); setChanged(true) },
  })
  const mismatch = form.again !== '' && form.again !== form.new

  return (
    <section className="card stack" style={{ maxWidth: 560, marginBottom: 16 }} aria-labelledby="password-title">
      <h2 id="password-title" style={{ margin: 0 }}>Password</h2>
      <form className="stack" onSubmit={(event) => { event.preventDefault(); if (!mismatch) change.mutate() }}>
        <label className="field">Current password
          <input type="password" autoComplete="current-password" required value={form.current}
            onChange={(event) => { setForm({ ...form, current: event.target.value }); setChanged(false) }} />
        </label>
        <label className="field">New password, at least 12 characters
          <input type="password" autoComplete="new-password" required minLength={12} value={form.new}
            onChange={(event) => setForm({ ...form, new: event.target.value })} />
        </label>
        <label className="field">New password again
          <input type="password" autoComplete="new-password" required value={form.again} aria-invalid={mismatch}
            onChange={(event) => setForm({ ...form, again: event.target.value })} />
        </label>
        {mismatch && <p className="error" role="alert">The new passwords differ.</p>}
        {change.error && <p className="error" role="alert">{change.error.message}</p>}
        {changed && <p role="status" style={{ margin: 0 }}>Password changed. You’re signed out everywhere else.</p>}
        <div><button type="submit" className="btn primary" disabled={change.isPending || mismatch}>{change.isPending ? 'Changing…' : 'Change password'}</button></div>
      </form>
    </section>
  )
}

function TwoFactorOff() {
  const client = useQueryClient()
  const [settingUp, setSettingUp] = useState(false)
  const [codes, setCodes] = useState<string[] | null>(null)

  if (codes) return <RecoveryCodes codes={codes} onDone={() => client.invalidateQueries({ queryKey: ['me'] })} />
  if (settingUp) return <TwoFactorSetup onConfirmed={setCodes} />

  return (
    <>
      <p style={{ margin: 0 }}>Ask for a code from an authenticator app at each sign-in, as well as your password, so a password alone signs no one in as you.</p>
      <div><button type="button" className="btn primary" onClick={() => setSettingUp(true)}>Set up</button></div>
    </>
  )
}

function TwoFactorOn() {
  const client = useQueryClient()
  const [action, setAction] = useState<'codes' | 'off' | null>(null)
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const newCodes = useMutation({
    mutationFn: () => api<{ recoveryCodes: string[] }>('/auth/2fa/recovery-codes', { method: 'POST', body: { code } }),
    onSuccess: (answer) => { setCodes(answer.recoveryCodes); setAction(null); setCode('') },
  })
  const turnOff = useMutation({
    mutationFn: () => api<User>('/auth/2fa/off', { method: 'POST', body: { password, code } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['me'] }),
  })
  const pending = action === 'codes' ? newCodes : turnOff

  if (codes) return <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />

  return (
    <>
      <p style={{ margin: 0 }}>Each sign-in with your password asks for a code from your authenticator app.</p>
      {action === null ? (
        <div className="row">
          <button type="button" className="btn" onClick={() => setAction('codes')}>New recovery codes</button>
          <button type="button" className="btn danger" onClick={() => setAction('off')}>Turn off</button>
        </div>
      ) : (
        <form className="stack" onSubmit={(event) => { event.preventDefault(); pending.mutate() }}>
          <p className="small" style={{ margin: 0 }}>
            {action === 'codes' ? 'Your old recovery codes stop working.' : 'Your password alone will sign you in again.'}
          </p>
          {action === 'off' && (
            <label className="field">Password
              <input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
            </label>
          )}
          <CodeInput value={code} onChange={setCode} label="Code from your app, or a recovery code" recovery />
          {pending.error && <p className="error" role="alert">{pending.error.message}</p>}
          <div className="row">
            <button type="button" className="btn" onClick={() => { setAction(null); setCode(''); setPassword('') }}>Cancel</button>
            <button type="submit" className={`btn ${action === 'off' ? 'danger' : 'primary'}`} disabled={pending.isPending}>
              {action === 'codes' ? 'Make new codes' : 'Turn off'}
            </button>
          </div>
        </form>
      )}
    </>
  )
}
