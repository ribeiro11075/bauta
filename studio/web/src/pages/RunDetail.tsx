import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, can, type Launched, type Run, type RunDetail } from '../api'
import { useUser } from '../App'
import { Failure, JobStatus, jobStages, Loading, PageHead, RunStatus, StageBar, StageKey, StartRunButton } from '../components/ui'
import { count, duration, plural, when, whenExactly } from '../format'

export function RunPage() {
  const { runId = '' } = useParams()
  const user = useUser()
  const run = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api<RunDetail>(`/runs/${runId}`),
    refetchInterval: (query) => (query.state.data?.live ? 2000 : false),
  })

  const crumb = <><Link to="/runs">Runs</Link> / {run.data ? when(run.data.startedAt) : runId.slice(0, 8)}</>
  if (run.isPending) return <><PageHead title="Run" crumb={crumb} /><Loading /></>
  if (run.error) return <><PageHead title="Run" crumb={crumb} /><Failure error={run.error} /></>

  const data = run.data
  const decided = (data.counts.completed ?? 0) + (data.counts.failed ?? 0) + (data.counts.skipped ?? 0)
  const failedJobs = data.jobs.filter((job) => job.status === 'failed' || job.status === 'skipped').map((job) => job.job)

  return (
    <>
      <PageHead title={`Run · ${when(data.startedAt)}`} crumb={crumb}
        sub={<>{data.launched ? `Started in Studio by ${data.launched.email}` : 'Started by the schedule'} · {plural(data.jobCount, 'job')}
          {data.host ? <> · on {data.host}</> : null} · run <span className="mono">{data.runId}</span></>}>
        <RunStatus status={data.status} counts={data.counts} />
        {!data.live && <CompareWith runId={data.runId} startedAt={data.startedAt} />}
        {!data.live && <a className="btn" href={`/api/runs/${data.runId}/evidence`} download>Download evidence</a>}
        {can(user, 'operator') && !data.live && failedJobs.length > 0 && <StartRunButton jobs={failedJobs} label="Rerun what failed" primary={false} />}
        {can(user, 'operator') && data.live && data.status !== 'stopped' && !data.cancelledBy && <CancelButton path={`/runs/${data.runId}/cancel`} />}
      </PageHead>

      {data.cancelledBy && (
        <div className="banner warn" role="status">
          {data.live ? 'Stopping' : 'Cancelled'}: {data.cancelledBy} asked it to stop. {data.live ? 'Running jobs finish; jobs not yet started are skipped.' : 'Jobs that had not started were skipped.'}
        </div>
      )}

      {data.status === 'stopped' && (
        <div className="banner warn" role="alert">The process running this ended without finishing it: killed, or its machine stopped. Jobs shown running were cut off; history has no record of them.</div>
      )}

      {data.live && (
        <div className="card">
          <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
            <strong>{decided} of {data.jobCount} jobs done</strong>
            <span className="muted">{duration(data.durationSeconds)} so far</span>
          </div>
          <div className="progress" role="progressbar" aria-valuenow={decided} aria-valuemin={0} aria-valuemax={data.jobCount} aria-label="Run progress">
            <span style={{ width: `${(decided / Math.max(1, data.jobCount)) * 100}%` }} />
          </div>
        </div>
      )}

      <div className="stats">
        <div className="card"><div className="label">Rows copied</div><div className="big mono">{count(data.rowCount)}</div></div>
        <div className="card"><div className="label">Duration</div><div className="big">{duration(data.durationSeconds)}</div></div>
        <div className="card"><div className="label">Completed</div><div className="big">{data.counts.completed ?? 0}</div></div>
        <div className="card"><div className="label">Failed or skipped</div><div className={`big ${failedJobs.length ? 'bad' : ''}`}>{failedJobs.length}</div></div>
      </div>

      <section>
        <div className="head" style={{ marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>Jobs</h2>
          <StageKey />
        </div>
        <div className="tablebox">
          <table>
            <thead><tr><th>Job</th><th>Started</th><th className="num">Rows</th><th className="num">Duration</th><th>Where the time went</th><th>Status</th></tr></thead>
            <tbody>
              {data.jobs.map((job) => (
                <tr key={job.job}>
                  <td><Link className="mono" to={`/jobs/${job.job}`}>{job.job}</Link>
                    {job.error && <div className="small error" style={{ maxWidth: 520, whiteSpace: 'normal' }}>{job.error}</div>}</td>
                  <td title={whenExactly(job.startedAt)}>{when(job.startedAt)}</td>
                  <td className="num mono">{count(job.rowCount)}{job.status === 'running' && <span className="small"> so far</span>}</td>
                  <td className="num">{job.durationSeconds !== undefined ? duration(job.durationSeconds) : '—'}</td>
                  <td style={{ minWidth: 180 }}><StageBar stages={jobStages(job)} label={`Job ${job.job}`} /></td>
                  <td><JobStatus status={job.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {data.launched && <RunLog log={data.log} />}
    </>
  )
}

// Picks another finished run to set this one against: the one before it to
// begin with, which is what "why is tonight slower" wants.
function CompareWith({ runId, startedAt }: { runId: string; startedAt: string | null }) {
  const navigate = useNavigate()
  const runs = useQuery({ queryKey: ['runs'], queryFn: () => api<{ runs: Run[] }>('/runs') })
  const others = (runs.data?.runs ?? []).filter((run) => !run.live && run.runId !== runId)
  const [chosen, setChosen] = useState('')
  if (!others.length) return null
  const previous = others.find((run) => (run.startedAt ?? '') < (startedAt ?? '')) ?? others[0]
  const other = others.find((run) => run.runId === chosen) ?? previous
  const [earlier, later] = (other.startedAt ?? '') < (startedAt ?? '') ? [other.runId, runId] : [runId, other.runId]
  return (
    <div className="row" style={{ gap: 6 }}>
      <label className="visually-hidden" htmlFor="compare-with">Compare with</label>
      <select id="compare-with" value={other.runId} onChange={(event) => setChosen(event.target.value)}>
        {others.slice(0, 30).map((run) => <option key={run.runId} value={run.runId}>{when(run.startedAt)}{run === previous ? ' (the run before)' : ''}</option>)}
      </select>
      <button type="button" className="btn" onClick={() => navigate(`/runs/compare?earlier=${earlier}&later=${later}`)}>Compare</button>
    </div>
  )
}

// Cancelling stops what hasn't started and lets what is running finish, so
// no target is left half loaded: what Ctrl-C does to `bauta run`.
function CancelButton({ path }: { path: string }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const client = useQueryClient()
  const cancel = useMutation({
    mutationFn: () => api(path, { method: 'POST' }),
    onSuccess: () => { dialog.current?.close(); client.invalidateQueries() },
  })
  return (
    <>
      <button type="button" className="btn danger" onClick={() => dialog.current?.showModal()}>Cancel run</button>
      <dialog ref={dialog} aria-labelledby="cancel-title">
        <form method="dialog" onSubmit={(event) => { event.preventDefault(); cancel.mutate() }}>
          <h2 id="cancel-title" style={{ margin: 0 }}>Cancel this run?</h2>
          <p>Jobs not yet started are skipped. Jobs already loading finish first, so no target is left half loaded: a run of one long job stops only when that job is done.</p>
          {cancel.error && <p className="error" role="alert">{cancel.error.message}</p>}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn" onClick={() => dialog.current?.close()}>Keep running</button>
            <button type="submit" className="btn primary" disabled={cancel.isPending} style={{ background: 'var(--bad)', borderColor: 'var(--bad)' }}>Cancel run</button>
          </div>
        </form>
      </dialog>
    </>
  )
}

function RunLog({ log }: { log: string | null | undefined }) {
  return (
    <section>
      <h2>Log</h2>
      <p className="small" style={{ marginBottom: 8 }}>What <code>bauta run</code> printed. Values bauta's scrubbing knows of are replaced with &lt;redacted&gt;.</p>
      <div className="log" role="log" aria-label="Run log" tabIndex={0}>{log || 'Nothing logged yet.'}</div>
    </section>
  )
}

// A run Studio started, until it names its run id; then its run's page.
export function LaunchedPage() {
  const { launchedId = '' } = useParams()
  const user = useUser()
  const navigate = useNavigate()
  const launched = useQuery({
    queryKey: ['launched', launchedId],
    queryFn: () => api<Launched>(`/launched/${launchedId}`),
    refetchInterval: (query) => (query.state.data?.finishedAt ? false : 1000),
  })

  useEffect(() => {
    if (launched.data?.runId) navigate(`/runs/${launched.data.runId}`, { replace: true })
  }, [launched.data?.runId, navigate])

  const crumb = <><Link to="/runs">Runs</Link> / starting</>
  if (launched.isPending) return <><PageHead title="Starting a run" crumb={crumb} /><Loading /></>
  if (launched.error) return <><PageHead title="Starting a run" crumb={crumb} /><Failure error={launched.error} /></>

  const data = launched.data
  return (
    <>
      <PageHead title={data.finishedAt ? 'The run did not start' : 'Starting a run…'} crumb={crumb}
        sub={<>Started by {data.email} · {data.jobs.length ? data.jobs.join(', ') : 'every due job'}</>}>
        {!data.finishedAt && can(user, 'operator') && <CancelButton path={`/launched/${data.id}/cancel`} />}
      </PageHead>
      {data.finishedAt && (
        <div className="banner bad" role="alert">
          It ended before its first cycle, with exit code {data.exitCode ?? 'unknown'}. The log says why: often another run holding the same jobs, or a configuration problem.
        </div>
      )}
      <RunLog log={data.log} />
    </>
  )
}
