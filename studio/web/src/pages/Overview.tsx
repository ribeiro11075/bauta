import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, can, type AuditReport, type CoverageReport, type Finding, type JobRecord, type ManifestState, type News, type Run, type ScheduleEntry } from '../api'
import { useUser } from '../App'
import { Failure, Loading, PageHead, Pill, Prose, RunStatus, StageBar, StageKey, StartRunButton } from '../components/ui'
import { ago, count, duration, plural, when } from '../format'
import { MANIFEST_STATES } from './Proof'

interface OverviewData {
  historyKept: boolean
  runs: Run[]
  jobCount: number
  activeJobCount: number
  failing: JobRecord[]
  audit: { summary: AuditReport['summary']; findings: Finding[] }
  manifest: ManifestState
  coverage: { report: CoverageReport; at: string } | null
  stale: ScheduleEntry[]
  news: News[]
}

export function Overview() {
  const user = useUser()
  const overview = useQuery({ queryKey: ['overview'], queryFn: () => api<OverviewData>('/overview'), refetchInterval: 5000 })

  return (
    <>
      <PageHead title="Overview" sub="Masked copies of production, and what happened to them.">
        <Link className="btn" to="/proof">Proof</Link>
        {can(user, 'operator') && <StartRunButton />}
      </PageHead>
      {overview.isPending ? <Loading /> : overview.error ? <Failure error={overview.error} /> : <OverviewBody data={overview.data} />}
    </>
  )
}

function OverviewBody({ data }: { data: OverviewData }) {
  const live = data.runs.filter((run) => run.live)
  const finished = data.runs.filter((run) => !run.live)
  const last = finished[0]
  const manifest = MANIFEST_STATES[data.manifest.state]
  const uncovered = data.coverage?.report.summary.uncovered ?? null

  return (
    <>
      {!data.historyKept && (
        <div className="banner warn" role="status">
          <span>Run history isn't kept, so Studio can't show past runs. Add <code>history: history.jsonl</code> to jobs.yaml.</span>
          <Link to="/configuration">Edit configuration</Link>
        </div>
      )}

      <div className="stats">
        <div className="card">
          <div className="label">{live.length ? 'Running now' : 'Last run'}</div>
          {live.length ? (
            <>
              <div className="big">{plural(live.length, 'run')}</div>
              <div className="muted"><Link to={`/runs/${live[0].runId}`}>Started {ago(live[0].startedAt)}</Link></div>
            </>
          ) : last ? (
            <>
              <div className={`big ${last.status === 'succeeded' ? '' : 'bad'}`}>
                {last.status === 'succeeded' ? 'Succeeded' : last.status === 'partial' ? `${(last.counts.failed ?? 0) + (last.counts.skipped ?? 0)} of ${last.jobCount} failed` : 'Failed'}
              </div>
              <div className="muted">{when(last.startedAt)} · {duration(last.durationSeconds)} · <Link to={`/runs/${last.runId}`}>See the run</Link></div>
            </>
          ) : <div className="big muted">None yet</div>}
        </div>
        <div className="card">
          <div className="label">Failing jobs</div>
          <div className={`big ${data.failing.length ? 'bad' : ''}`}>{data.failing.length}</div>
          <div className="muted">{data.failing.length ? data.failing.map((job) => job.job).join(', ') : `of ${plural(data.activeJobCount, 'active job')}`}</div>
        </div>
        <div className="card">
          <div className="label">Audit of masking policies</div>
          <div className={`big ${data.audit.summary.error ? 'bad' : ''}`}>{plural(data.audit.summary.error, 'error')}</div>
          <div className="muted">{plural(data.audit.summary.warning, 'warning')} · from the configuration</div>
        </div>
        <div className="card">
          <div className="label">Manifest of the last run</div>
          <div className={`big ${manifest.kind === 'bad' ? 'bad' : ''}`}>{manifest.label}</div>
          <div className="muted">{data.manifest.generatedAt ? when(data.manifest.generatedAt) : manifest.hint}</div>
        </div>
      </div>

      <div className="two">
        <section className="card" aria-labelledby="attention">
          <h2 id="attention">Needs attention</h2>
          <Attention data={data} uncovered={uncovered} />
        </section>
        <section className="card" aria-labelledby="time">
          <h2 id="time">Where the time went, recent runs</h2>
          {finished.length ? (
            <div className="stack">
              {finished.slice(0, 7).map((run) => (
                <div className="row" key={run.runId} style={{ flexWrap: 'nowrap' }}>
                  <Link style={{ flex: 'none', whiteSpace: 'nowrap', minWidth: 120 }} to={`/runs/${run.runId}`}>{when(run.startedAt)}</Link>
                  <StageBar stages={run.stages} label={`Run of ${when(run.startedAt)}`} />
                </div>
              ))}
              <StageKey />
              <p className="small">Each bar is a run's busy time per stage, added over its jobs. Stages overlap, so the longest is what set the pace.</p>
            </div>
          ) : <p className="muted">Nothing recorded yet.</p>}
        </section>
      </div>

      <section aria-labelledby="recent">
        <div className="head" style={{ marginBottom: 10 }}>
          <h2 id="recent" style={{ margin: 0 }}>Recent runs</h2>
          <Link to="/runs">All runs</Link>
        </div>
        <RunTable runs={data.runs.slice(0, 6)} />
      </section>
    </>
  )
}

function Attention({ data, uncovered }: { data: OverviewData; uncovered: number | null }) {
  const items = [
    ...data.news.map((entry, index) => (
      <div className="item" key={`news-${index}`}>
        <Pill kind={entry.kind === 'references' ? 'bad' : 'warn'}>New</Pill>
        <div>
          {entry.kind === 'coverage' && <>A table no job covers in <span className="mono">{entry.database}</span>: <span className="mono">{entry.table}</span>
            {entry.personalDataColumns?.length ? <>, which looks personal (<span className="mono">{entry.personalDataColumns.join(', ')}</span>)</> : null}. <Link to="/proof">Copy or declare it</Link></>}
          {entry.kind === 'discovery' && <><Link className="mono" to={`/jobs/${entry.job}`}>{entry.job}</Link> returns columns its policy doesn't name: <span className="mono">{entry.columns?.join(', ')}</span>. <Link to="/discovery">Review</Link></>}
          {entry.kind === 'references' && <><span className="mono">{entry.reference}</span>: {count(entry.orphans)} rows point at nothing. <Link to="/proof">See references</Link></>}
          <div className="small">Found {ago(entry.at)} by {entry.by === 'schedule' ? 'the nightly checks' : entry.by}</div>
        </div>
      </div>
    )),
    ...data.failing.map((job) => (
      <div className="item" key={`job-${job.job}`}>
        <Pill kind={job.status === 'failed' ? 'bad' : 'warn'}>{job.status === 'failed' ? 'Failed' : 'Skipped'}</Pill>
        <div><Link className="mono" to={`/jobs/${job.job}`}>{job.job}</Link>: {job.error?.slice(0, 220)}{job.runId && <> <Link to={`/runs/${job.runId}`}>See the run</Link></>}</div>
      </div>
    )),
    ...data.stale.map((entry) => (
      <div className="item" key={`stale-${entry.job}`}>
        <Pill kind="bad">{entry.neverCompleted ? 'Never run' : 'Stale'}</Pill>
        <div><Link className="mono" to={`/jobs/${entry.job}`}>{entry.job}</Link>: {entry.neverCompleted ? 'has never completed.' : `last completed ${ago(entry.lastCompletedAt)}, more than ${entry.staleAfterHours} h ago.`} <Link to="/schedule">Schedule</Link></div>
      </div>
    )),
    ...data.audit.findings.map((finding, index) => (
      <div className="item" key={`finding-${index}`}>
        <Pill kind={finding.severity === 'error' ? 'bad' : 'warn'}>{finding.severity === 'error' ? 'Audit error' : 'Audit'}</Pill>
        <div>{finding.job && <><Link className="mono" to={`/jobs/${finding.job}`}>{finding.job}</Link>: </>}<Prose text={finding.message} /></div>
      </div>
    )),
  ]
  // Said once: by what the last check found new, when it found anything.
  if (uncovered && !data.news.some((entry) => entry.kind === 'coverage')) {
    items.push(
      <div className="item" key="coverage">
        <Pill kind="warn">Coverage</Pill>
        <div>{plural(uncovered, 'table')} no job copies or declares. <Link to="/proof">Copy or declare them</Link></div>
      </div>,
    )
  }
  if (['altered', 'signature-invalid'].includes(data.manifest.state)) {
    items.unshift(
      <div className="item" key="manifest">
        <Pill kind="bad">Manifest</Pill>
        <div>The last manifest does not verify: it was changed after it was written. <Link to="/proof">See proof</Link></div>
      </div>,
    )
  }

  return items.length ? <>{items.slice(0, 8)}</> : <p className="muted">Nothing. Every job's last run completed and the audit found no errors or warnings.</p>
}

export function RunTable({ runs }: { runs: Run[] }) {
  if (!runs.length) return <div className="tablebox"><div className="empty">No runs recorded yet.</div></div>
  return (
    <div className="tablebox">
      <table>
        <thead>
          <tr><th>Started</th><th>Started by</th><th className="num">Jobs</th><th className="num">Rows</th><th className="num">Duration</th><th>Where the time went</th><th>Status</th></tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.runId}>
              <td style={{ whiteSpace: 'nowrap' }}><Link to={`/runs/${run.runId}`}>{when(run.startedAt)}</Link></td>
              <td>{run.startedBy ?? <span className="muted">schedule</span>}</td>
              <td className="num">{(run.counts.completed ?? 0)} of {run.jobCount}</td>
              <td className="num mono">{count(run.rowCount)}</td>
              <td className="num">{duration(run.durationSeconds)}{run.live ? ' so far' : ''}</td>
              <td style={{ minWidth: 180 }}><StageBar stages={run.stages} label={`Run of ${when(run.startedAt)}`} /></td>
              <td><RunStatus status={run.status} counts={run.counts} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
