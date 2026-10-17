import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, can, type DiscoveryCheck, type Written } from '../api'
import { useUser } from '../App'
import { DiffView, Failure, Loading, PageHead, Pill } from '../components/ui'
import { ago } from '../format'

function policyText(policy: Record<string, unknown>): string {
  const { strategy, ...options } = policy
  const rest = Object.entries(options).map(([name, value]) => `${name}: ${JSON.stringify(value)}`).join(', ')
  return rest ? `${strategy} (${rest})` : String(strategy)
}

export function Discovery() {
  const user = useUser()
  const client = useQueryClient()
  const discovery = useQuery({ queryKey: ['discovery'], queryFn: () => api<{ check: DiscoveryCheck | null }>('/discovery') })
  const [accepted, setAccepted] = useState<string[]>([])
  const [written, setWritten] = useState<Written[] | null>(null)
  const check = useMutation({
    mutationFn: () => api<{ check: DiscoveryCheck }>('/discovery/check', { method: 'POST' }),
    onSuccess: (result) => { client.setQueryData(['discovery'], result); setAccepted(result.check.jobs.map((job) => job.job)); setWritten(null) },
  })
  const apply = useMutation({
    mutationFn: () => api<{ written: Written[] }>('/discovery/apply', { method: 'POST', body: { jobs: accepted } }),
    onSuccess: (result) => { setWritten(result.written); client.invalidateQueries({ predicate: (query) => query.queryKey[0] !== 'discovery' }) },
  })
  const kept = discovery.data?.check

  return (
    <>
      <PageHead title="Discovery review" sub={<>Columns a source now returns that a job's policy doesn't name, with the policy <code>discover</code> proposes for each. Nothing changes until an admin accepts it.</>}>
        {can(user, 'operator') && <button type="button" className="btn primary" disabled={check.isPending} onClick={() => check.mutate()}>{check.isPending ? 'Checking the sources…' : 'Check for new columns'}</button>}
      </PageHead>
      {check.error && <Failure error={check.error} />}
      {discovery.isPending ? <Loading /> : discovery.error ? <Failure error={discovery.error} /> : !kept ? (
        <div className="card empty">Not checked yet. {can(user, 'operator') ? 'Checking reads each masked job\'s query and a sample of its rows; no value leaves the server.' : 'An operator can check.'}</div>
      ) : (
        <>
          <p className="muted">Checked {ago(kept.at)} by {kept.by}: {kept.checkedJobs} masked jobs, {kept.jobs.length} with columns their policy doesn't match.</p>
          {written && (
            <div className="banner ok" role="status"><div><strong>Accepted.</strong> The next run masks the new columns as proposed. Review them in the job's form or the editor.
              {written.map((write) => <DiffView key={write.path} diff={write.diff} />)}</div></div>
          )}
          {apply.error && <Failure error={apply.error} />}
          {kept.jobs.length === 0 ? <div className="banner ok">Every masked job's policy names exactly the columns its query returns.</div> : (
            <>
              {kept.jobs.map((drift) => (
                <section className="card" key={drift.job}>
                  <div className="head" style={{ marginBottom: 10 }}>
                    <h2 style={{ margin: 0 }}>
                      {can(user, 'admin') && <input type="checkbox" aria-label={`Accept the proposals for ${drift.job}`} checked={accepted.includes(drift.job)} style={{ minHeight: 0, marginRight: 8 }}
                        onChange={() => setAccepted(accepted.includes(drift.job) ? accepted.filter((job) => job !== drift.job) : [...accepted, drift.job])} />}
                      <Link className="mono" to={`/jobs/${drift.job}`}>{drift.job}</Link>
                    </h2>
                    <span className="small">in <code>{drift.file}</code></span>
                  </div>
                  <div className="tablebox">
                    <table>
                      <thead><tr><th>Column</th><th>Change</th><th>Proposed policy</th><th>Why</th></tr></thead>
                      <tbody>
                        {drift.added.map((added) => (
                          <tr key={added.column}>
                            <td className="mono">{added.column}</td><td><Pill kind="warn">New</Pill></td>
                            <td>{added.policy.strategy === 'keep' ? <Pill kind="warn">keep</Pill> : <code>{policyText(added.policy)}</code>}</td>
                            <td className="wrap small">{added.reason}</td>
                          </tr>
                        ))}
                        {drift.removed.map((column) => (
                          <tr key={column}><td className="mono">{column}</td><td><Pill kind="idle">Gone</Pill></td><td className="muted">removed from the policy</td><td className="small">no longer returned by its query</td></tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              ))}
              {can(user, 'admin') && (
                <div className="row" style={{ justifyContent: 'flex-end' }}>
                  <span className="small">Written into each job's file, validated, kept and recorded like any edit.</span>
                  <button type="button" className="btn primary" disabled={!accepted.length || apply.isPending} onClick={() => apply.mutate()}>
                    {apply.isPending ? 'Validating…' : `Accept ${accepted.length} job${accepted.length === 1 ? '' : 's'}`}
                  </button>
                </div>
              )}
            </>
          )}
        </>
      )}
    </>
  )
}
