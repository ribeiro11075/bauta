// Two-factor sign-in's pieces, shared by the sign-in page, where someone whose
// role needs it sets it up before signing in, and the Account page.
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, signingIn } from '../api'

interface SetupDetails { secret: string; uri: string; qr: string }

// A six-digit code, or a recovery code: what an input for either accepts.
export function CodeInput({ value, onChange, label = 'Code from your authenticator app', recovery = false }:
  { value: string; onChange: (value: string) => void; label?: string; recovery?: boolean }) {
  return (
    <label className="field">{label}
      <input autoComplete="one-time-code" inputMode={recovery ? 'text' : 'numeric'} required autoFocus spellCheck={false}
        value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  )
}

// Scanning the QR code, or typing the key, then a code to prove the app has it.
export function TwoFactorSetup({ onConfirmed }: { onConfirmed: (recoveryCodes: string[]) => void }) {
  const [code, setCode] = useState('')
  const [showKey, setShowKey] = useState(false)
  // Fetched once: each request makes a new secret.
  const setup = useQuery({ ...signingIn, queryKey: ['2fa-setup'], queryFn: () => api<SetupDetails>('/auth/2fa/setup', { method: 'POST' }),
    staleTime: Infinity, gcTime: 0, retry: false })
  const confirm = useMutation({
    ...signingIn,
    mutationFn: () => api<{ recoveryCodes: string[] }>('/auth/2fa/confirm', { method: 'POST', body: { code } }),
    onSuccess: (answer) => onConfirmed(answer.recoveryCodes),
  })

  if (setup.isPending) return <p className="muted">Preparing…</p>
  if (setup.error) return <p className="error" role="alert">{setup.error.message}</p>

  return (
    <form className="stack" onSubmit={(event) => { event.preventDefault(); confirm.mutate() }}>
      <p style={{ margin: 0 }}>Scan this with an authenticator app, such as 1Password, Google Authenticator or Microsoft Authenticator.</p>
      <img className="qr" src={setup.data.qr} alt="QR code to set up your authenticator app" width={200} height={200} />
      {showKey ? (
        <p className="small" style={{ margin: 0 }}>Or type this key: <code className="secret">{setup.data.secret.match(/.{1,4}/g)?.join(' ')}</code></p>
      ) : (
        <button type="button" className="btn link" onClick={() => setShowKey(true)}>Can’t scan it? Type a key instead</button>
      )}
      <CodeInput value={code} onChange={setCode} label="Then the code the app shows" />
      {confirm.error && <p className="error" role="alert">{confirm.error.message}</p>}
      <button type="submit" className="btn primary" disabled={confirm.isPending}>{confirm.isPending ? 'Checking…' : 'Turn on'}</button>
    </form>
  )
}

// Shown once: each stands in for a code from the app, once, if it is lost.
export function RecoveryCodes({ codes, onDone, doneLabel = 'Done' }: { codes: string[]; onDone: () => void; doneLabel?: string }) {
  const [saved, setSaved] = useState(false)
  const [copied, setCopied] = useState(false)

  return (
    <div className="stack">
      <p style={{ margin: 0 }}>Save these recovery codes somewhere safe, such as your password manager. Each signs you in once if you lose your app. They aren’t shown again.</p>
      <ul className="codes" aria-label="Recovery codes">{codes.map((code) => <li key={code}><code>{code}</code></li>)}</ul>
      <div className="row">
        <button type="button" className="btn sm" onClick={() => { void navigator.clipboard?.writeText(codes.join('\n')).then(() => setCopied(true)) }}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <label className="check"><input type="checkbox" checked={saved} onChange={(event) => setSaved(event.target.checked)} /> I’ve saved them</label>
      <button type="button" className="btn primary" disabled={!saved} onClick={onDone}>{doneLabel}</button>
    </div>
  )
}
