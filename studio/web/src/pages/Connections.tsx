import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, can, type Connection } from '../api'
import { useUser } from '../App'
import { Failure, Loading, PageHead, Pill } from '../components/ui'

export function Connections() {
  const user = useUser()
  const connections = useQuery({ queryKey: ['connections'], queryFn: () => api<{ connections: Connection[] }>('/connections') })

  return (
    <>
      <PageHead title="Connections" sub={<>From <code>connections.yaml</code>. Credentials stay on the server, named there by environment variable or passwordCommand; Studio never shows or edits them.</>} />
      {connections.isPending ? <Loading /> : connections.error ? <Failure error={connections.error} /> : (
        <div className="tablebox">
          <table>
            <thead><tr><th>Alias</th><th>Type</th><th>Where</th><th>Rules</th><th>Used by</th>{can(user, 'operator') && <th>Check</th>}</tr></thead>
            <tbody>
              {connections.data.connections.map((connection) => <ConnectionRow key={connection.alias} connection={connection} canTest={can(user, 'operator')} />)}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

function ConnectionRow({ connection, canTest }: { connection: Connection; canTest: boolean }) {
  const [shown, setShown] = useState(false)
  const test = useMutation({
    mutationFn: () => api<{ ok: boolean; detail: string }>(`/connections/${connection.alias}/test`, { method: 'POST' }),
    onSettled: () => setShown(true),
  })
  const jobs = [...connection.sourceOf.map((job) => ['source', job]), ...connection.targetOf.map((job) => ['target', job])]

  return (
    <tr>
      <td className="mono">{connection.alias}</td>
      <td>{connection.type}</td>
      <td className="mono wrap">{connection.target}</td>
      <td>
        <div className="row" style={{ gap: 6 }}>
          {connection.readOnly && <Pill kind="idle">read-only</Pill>}
          {connection.requireMasking && <Pill kind="ok">requires masking</Pill>}
          {connection.requireEncryption && <Pill kind="ok">requires encryption</Pill>}
          {!connection.readOnly && !connection.requireMasking && !connection.requireEncryption && <span className="muted">—</span>}
        </div>
      </td>
      <td className="wrap small">
        {connection.sourceOf.length ? <>reads for {connection.sourceOf.length} · </> : null}
        {connection.targetOf.length ? <>loads {connection.targetOf.length}</> : null}
        {!jobs.length && 'no job'}
        {jobs.length > 0 && <div>{jobs.slice(0, 4).map(([, job]) => <Link key={job} className="mono" to={`/jobs/${job}`} style={{ marginRight: 8 }}>{job}</Link>)}{jobs.length > 4 ? '…' : ''}</div>}
      </td>
      {canTest && (
        <td>
          <div className="stack" style={{ gap: 6 }}>
            <button type="button" className="btn sm" disabled={test.isPending} onClick={() => test.mutate()}>{test.isPending ? 'Connecting…' : 'Test'}</button>
            {shown && test.data && <span role="status" className={test.data.ok ? 'small' : 'small error'}>{test.data.detail}</span>}
            {shown && test.error && <span role="status" className="small error">{test.error.message}</span>}
          </div>
        </td>
      )}
    </tr>
  )
}
