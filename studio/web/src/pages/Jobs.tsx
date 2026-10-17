import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, can, type Job } from '../api'
import { useUser } from '../App'
import { Failure, JobStatus, jobStages, Loading, PageHead, Pill, StageBar, StartRunButton } from '../components/ui'
import { JobTrends } from '../components/TrendChart'
import { BenchCard } from '../components/Bench'
import { count, duration, when } from '../format'

export function Jobs() {
  const user = useUser()
  const [search, setSearch] = useState('')
  const [target, setTarget] = useState('')
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => api<{ jobs: Job[]; files: { path: string; jobs: string[] }[] }>('/jobs') })

  const targets = useMemo(() => [...new Set((jobs.data?.jobs ?? []).map((job) => job.targetConnection))].sort(), [jobs.data])
  const shown = (jobs.data?.jobs ?? []).filter((job) => {
    const text = `${job.name} ${job.targetTable} ${job.sourceConnection} ${job.targetConnection}`.toLowerCase()
    return text.includes(search.toLowerCase()) && (!target || job.targetConnection === target)
  })

  return (
    <>
      <PageHead title="Jobs" sub={<>From <code>jobs.yaml</code> and the files it includes, as Studio's server reads them.</>}>
        <Link className="btn" to="/configuration">Edit configuration</Link>
        {can(user, 'admin') && <Link className="btn primary" to="/jobs/new">New job</Link>}
      </PageHead>
      {jobs.isPending ? <Loading /> : jobs.error ? <Failure error={jobs.error} /> : (
        <>
          <div className="row" style={{ alignItems: 'flex-end' }}>
            <label className="field">Search jobs
              <input type="search" placeholder="Name, table or connection" style={{ minWidth: 260 }} value={search} onChange={(event) => setSearch(event.target.value)} />
            </label>
            <label className="field">Target
              <select value={target} onChange={(event) => setTarget(event.target.value)}>
                <option value="">All targets</option>
                {targets.map((alias) => <option key={alias}>{alias}</option>)}
              </select>
            </label>
          </div>
          <div className="tablebox">
            <table>
              <thead><tr><th>Job</th><th>Source → target table</th><th>Loads by</th><th>Incremental on</th><th>Masking</th><th>Last run</th></tr></thead>
              <tbody>
                {shown.map((job) => (
                  <tr key={job.name}>
                    <td><Link className="mono" to={`/jobs/${job.name}`}>{job.name}</Link></td>
                    <td className="mono">{job.sourceConnection} → {job.targetConnection}.{job.targetTable}</td>
                    <td>{job.insertStrategy}</td>
                    <td className="mono">{job.watermarkColumn ?? <span className="muted">—</span>}</td>
                    <td><MaskingSummary job={job} /></td>
                    <td>{!job.active ? <Pill kind="idle">Inactive</Pill> : job.lastRun ? <JobStatus status={job.lastRun.status} /> : <span className="muted">Not run</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!shown.length && <div className="empty">No job matches.</div>}
          </div>
          <p className="muted">Showing {shown.length} of {jobs.data.jobs.length} jobs.</p>
        </>
      )}
    </>
  )
}

function MaskingSummary({ job }: { job: Job }) {
  if (job.masking) {
    const masked = Object.values(job.masking.columns).filter((strategy) => strategy !== 'keep').length
    return <>{masked} masked{job.masking.defaultStrategy ? `, others ${job.masking.defaultStrategy}` : ''}</>
  }
  return job.unmasked ? <span className="muted">none, declared</span> : <Pill kind="bad">Not masked</Pill>
}

export function JobPage() {
  const { name = '' } = useParams()
  const user = useUser()
  const job = useQuery({ queryKey: ['job', name], queryFn: () => api<Job>(`/jobs/${name}`),
    refetchInterval: (query) => (query.state.data?.bench?.status === 'running' ? 1500 : false) })
  const crumb = <><Link to="/jobs">Jobs</Link> / {name}</>

  if (job.isPending) return <><PageHead title={name} crumb={crumb} /><Loading /></>
  if (job.error) return <><PageHead title={name} crumb={crumb} /><Failure error={job.error} /></>
  const data = job.data

  return (
    <>
      <PageHead title={<span className="mono" style={{ fontSize: 22 }}>{data.name}</span>} crumb={crumb}
        sub={<>{data.sourceConnection} → {data.targetConnection}.{data.targetTable}{data.file ? <> · defined in <code>{data.file}</code></> : null}</>}>
        {can(user, 'admin') && <Link className="btn" to={`/jobs/${data.name}/edit`}>Edit</Link>}
        {can(user, 'operator') && data.active && <StartRunButton jobs={[data.name]} label="Run now" />}
      </PageHead>

      <div className="two">
        <section className="card">
          <h2>How it loads</h2>
          <dl className="stack" style={{ margin: 0 }}>
            <Fact label="Loads by">{data.insertStrategy}{data.stageTable ? <>, through <code>{data.stageTable}</code></> : null}</Fact>
            <Fact label="Incremental on">{data.watermarkColumn ? <code>{data.watermarkColumn}</code> : 'no: the whole query each run'}</Fact>
            <Fact label="Partitions">{data.partitions ?? 'one stream'}</Fact>
            <Fact label="Waits for">{data.predecessors.length ? data.predecessors.map((predecessor) => <Link key={predecessor} className="mono" to={`/jobs/${predecessor}`} style={{ marginRight: 8 }}>{predecessor}</Link>) : 'nothing'}</Fact>
            <Fact label="Refresh">{data.refresh ?? 'every run'}</Fact>
            <Fact label="Active">{data.active ? 'yes' : 'no'}</Fact>
          </dl>
          <h2 style={{ marginTop: 16 }}>Source query</h2>
          <pre className="log" style={{ whiteSpace: 'pre-wrap', maxHeight: 220 }}>{data.sourceQuery}</pre>
        </section>
        <section className="card">
          <h2>Masking</h2>
          {data.masking ? (
            <>
              <p className="small" style={{ marginBottom: 10 }}>Under key <code>{data.masking.keyFingerprint}</code>, by fingerprint; the key itself never leaves the server.</p>
              <div className="tablebox">
                <table>
                  <thead><tr><th>Column</th><th>Strategy</th></tr></thead>
                  <tbody>
                    {Object.entries(data.masking.columns).map(([column, strategy]) => (
                      <tr key={column}><td className="mono">{column}</td><td>{strategy === 'keep' ? <Pill kind="warn">keep</Pill> : <code>{strategy}</code>}</td></tr>
                    ))}
                    {data.masking.defaultStrategy && <tr><td className="muted">any other column</td><td><code>{data.masking.defaultStrategy}</code></td></tr>}
                  </tbody>
                </table>
              </div>
            </>
          ) : data.unmasked ? <p>Copies as it stands, declared with <code>unmasked: true</code>.</p>
            : <div className="banner bad">Copies without masking, and doesn't declare it. <code>audit</code> reports this as an error.</div>}
        </section>
      </div>

      <JobTrends records={data.history ?? []} />
      <BenchCard job={data.name} bench={data.bench ?? null} />

      <section>
        <h2>Recent runs of this job</h2>
        <div className="tablebox">
          <table>
            <thead><tr><th>Finished</th><th className="num">Rows</th><th className="num">Duration</th><th>Where the time went</th><th>Status</th></tr></thead>
            <tbody>
              {(data.history ?? []).slice(0, 20).map((record) => (
                <tr key={`${record.runId}-${record.finishedAt}`}>
                  <td><Link to={`/runs/${record.runId}`}>{when(record.finishedAt)}</Link>
                    {record.error && <div className="small error" style={{ whiteSpace: 'normal', maxWidth: 520 }}>{record.error}</div>}</td>
                  <td className="num mono">{count(record.rowCount)}</td>
                  <td className="num">{duration(record.durationSeconds)}</td>
                  <td style={{ minWidth: 180 }}><StageBar stages={jobStages(record)} label={`Run of ${when(record.finishedAt)}`} /></td>
                  <td><JobStatus status={record.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data.history?.length && <div className="empty">No runs recorded.</div>}
        </div>
      </section>
    </>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ alignItems: 'baseline', flexWrap: 'nowrap' }}>
      <dt className="label" style={{ width: 120, flex: 'none' }}>{label}</dt>
      <dd style={{ margin: 0 }}>{children}</dd>
    </div>
  )
}
