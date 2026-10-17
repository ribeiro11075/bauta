import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { Failure, JobStatus, Loading, PageHead, RunStatus } from '../components/ui'
import { count, duration, seconds, when } from '../format'

type Pair<T> = { before: T | null; after: T | null }

interface Comparison {
  earlier: { runId: string; startedAt: string; status: string; rowCount: number; durationSeconds: number | null }
  later: { runId: string; startedAt: string; status: string; rowCount: number; durationSeconds: number | null }
  manifestsCompared: boolean
  jobs: { job: string; status: Pair<string>; rows: Pair<number>; duration: Pair<number>; stages: Record<string, Pair<number>>;
    columns: { added?: string[]; removed?: string[]; changed?: { column: string; before: string; after: string }[] } }[]
}

const STAGE_NAMES: Record<string, string> = { read: 'reading', mask: 'masking', write: 'writing', wait: 'waiting' }

// A change, with its sign and a word: never colour alone.
function Change({ before, after, format, worseWhenUp }: { before: number | null; after: number | null; format: (value: number) => string; worseWhenUp: boolean }) {
  if (before === null || after === null) return <span className="muted">{after === null ? '—' : format(after)}</span>
  const delta = after - before
  const share = before ? Math.round((delta / before) * 100) : null
  const notable = share !== null && Math.abs(share) >= 20
  const worse = notable && (worseWhenUp ? delta > 0 : delta < 0)
  return (
    <span>
      {format(after)}{' '}
      {delta !== 0 && <span className={worse ? 'error small' : 'small'}>({delta > 0 ? '+' : '−'}{format(Math.abs(delta))}{share !== null ? `, ${share > 0 ? '+' : ''}${share}%` : ''})</span>}
    </span>
  )
}

function slowestStage(stages: Record<string, Pair<number>>): string | null {
  const moved = Object.entries(stages).filter(([, pair]) => pair.before !== null && pair.after !== null).map(([stage, pair]) => [stage, (pair.after ?? 0) - (pair.before ?? 0)] as const)
  const worst = moved.sort((one, two) => two[1] - one[1])[0]
  return worst && worst[1] > 0.5 ? `${STAGE_NAMES[worst[0]]} +${duration(worst[1])}` : null
}

export function Compare() {
  const [params] = useSearchParams()
  const earlier = params.get('earlier') ?? ''
  const later = params.get('later') ?? ''
  const compared = useQuery({ queryKey: ['compare', earlier, later], queryFn: () => api<Comparison>('/runs/compare', { query: { earlier, later } }), enabled: !!(earlier && later) })

  const crumb = <><Link to="/runs">Runs</Link> / compare</>
  if (compared.isPending) return <><PageHead title="Compare runs" crumb={crumb} /><Loading /></>
  if (compared.error) return <><PageHead title="Compare runs" crumb={crumb} /><Failure error={compared.error} /></>
  const data = compared.data

  return (
    <>
      <PageHead title="Compare runs" crumb={crumb}
        sub={<><Link to={`/runs/${data.earlier.runId}`}>{when(data.earlier.startedAt)}</Link> against <Link to={`/runs/${data.later.runId}`}>{when(data.later.startedAt)}</Link>, job by job.</>} />
      <div className="stats">
        <div className="card"><div className="label">Rows</div><div className="big mono"><Change before={data.earlier.rowCount} after={data.later.rowCount} format={(value) => count(value)} worseWhenUp={false} /></div></div>
        <div className="card"><div className="label">Duration</div><div className="big"><Change before={data.earlier.durationSeconds} after={data.later.durationSeconds} format={seconds} worseWhenUp /></div></div>
        <div className="card"><div className="label">Status</div><div className="big row"><RunStatus status={data.earlier.status} /> → <RunStatus status={data.later.status} /></div></div>
      </div>
      <div className="tablebox">
        <table>
          <thead><tr><th>Job</th><th>Status</th><th className="num">Rows</th><th className="num">Duration</th><th>Slowed by</th><th>Masking</th></tr></thead>
          <tbody>
            {data.jobs.map((job) => {
              const columns = job.columns
              const maskingChanges = [...(columns.added ?? []).map((column) => `+${column}`), ...(columns.removed ?? []).map((column) => `−${column}`),
                ...(columns.changed ?? []).map((change) => `${change.column}: ${change.before} → ${change.after}`)]
              return (
                <tr key={job.job}>
                  <td><Link className="mono" to={`/jobs/${job.job}`}>{job.job}</Link></td>
                  <td>{job.status.before ? <JobStatus status={job.status.before} /> : <span className="muted">not run</span>} → {job.status.after ? <JobStatus status={job.status.after} /> : <span className="muted">not run</span>}</td>
                  <td className="num mono"><Change before={job.rows.before} after={job.rows.after} format={(value) => count(value)} worseWhenUp={false} /></td>
                  <td className="num"><Change before={job.duration.before} after={job.duration.after} format={seconds} worseWhenUp /></td>
                  <td className="small">{slowestStage(job.stages) ?? '—'}</td>
                  <td className="small mono">{!data.manifestsCompared ? <span className="muted">no manifest for one of them</span> : maskingChanges.length ? maskingChanges.join(', ') : 'unchanged'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="small">A change of 20% or more for the worse is marked: fewer rows, or a longer time. Masking compares the manifests the two runs sealed.</p>
    </>
  )
}
