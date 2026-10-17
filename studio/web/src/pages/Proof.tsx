import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { api, can, type AuditReport, type Check, type CheckSchedule, type CoverageReport, type ManifestState, type ReferencesReport } from '../api'
import { useUser } from '../App'
import { Failure, JobStatus, Loading, PageHead, Pill, Prose } from '../components/ui'
import { ago, count, plural, when } from '../format'

interface ProofData {
  audit: { report: AuditReport; connected: boolean; at: string | null; by?: string }
  offlineAudit: AuditReport
  coverages: Check<CoverageReport>[]
  sources: string[]
  references: Check<ReferencesReport> | null
  manifest: ManifestState
  manifests: { key: string; archivedAt: string | null; generatedAt: string | null; jobs: number;
    verification: { verified: boolean; signed?: boolean; signatureChecked?: boolean; problem?: string } }[]
}

export const MANIFEST_STATES: Record<ManifestState['state'], { label: string; kind: string; hint: string }> = {
  'verified': { label: 'Verified', kind: 'ok', hint: 'Intact, and signed with the key Studio checks against.' },
  'intact-unsigned': { label: 'Intact', kind: 'ok', hint: 'Unchanged since written. Unsigned, so it doesn\'t show who wrote it.' },
  'signed-unchecked': { label: 'Intact, signed', kind: 'ok', hint: 'Signed, but Studio has no BAUTA_MANIFEST_KEY to check the signature with.' },
  'unsigned-with-key': { label: 'Unsigned', kind: 'warn', hint: 'A signing key is set, yet the manifest is unsigned: a signature may have been removed.' },
  'altered': { label: 'Altered', kind: 'bad', hint: 'Its digest doesn\'t match: it was changed after it was written.' },
  'signature-invalid': { label: 'Signature invalid', kind: 'bad', hint: 'Not signed by this key, or changed after signing.' },
  'unreadable': { label: 'Unreadable', kind: 'bad', hint: 'The manifest could not be read.' },
  'not-written': { label: 'None yet', kind: 'idle', hint: 'No run has written one yet.' },
  'not-kept': { label: 'Not kept', kind: 'idle', hint: 'Add manifest: to jobs.yaml to seal what each run masked.' },
}

const COVERAGE_STATES: [string, string, string][] = [
  ['uncovered', 'Not covered', 'bad'],
  ['copied', 'Copied as it stands', 'warn'],
  ['masked', 'Copied and masked', 'ok'],
  ['acknowledged', 'Not copied, declared', 'idle'],
]

export function Proof() {
  const user = useUser()
  const client = useQueryClient()
  const proof = useQuery({ queryKey: ['proof'], queryFn: () => api<ProofData>('/proof') })
  const refresh = () => client.invalidateQueries({ queryKey: ['proof'] })
  const coverage = useMutation({ mutationFn: () => api('/proof/coverage', { method: 'POST', body: {} }), onSuccess: refresh })
  const audit = useMutation({ mutationFn: () => api('/proof/audit', { method: 'POST', body: { connect: true } }), onSuccess: refresh })
  const references = useMutation({ mutationFn: () => api('/proof/references', { method: 'POST' }), onSuccess: refresh })
  const dryRun = useMutation({ mutationFn: () => api<{ problems: string[]; at: string }>('/proof/dry-run', { method: 'POST' }) })
  const operator = can(user, 'operator')

  return (
    <>
      <PageHead title="Proof" sub="What a reviewer needs: every table accounted for, every policy checked, every run's manifest verified.">
        {operator && <button type="button" className="btn" disabled={dryRun.isPending} onClick={() => dryRun.mutate()}>{dryRun.isPending ? 'Checking…' : 'Dry run'}</button>}
        {operator && <button type="button" className="btn" disabled={references.isPending} onClick={() => references.mutate()}>{references.isPending ? 'Counting…' : 'Check references'}</button>}
        {operator && <button type="button" className="btn" disabled={audit.isPending} onClick={() => audit.mutate()}>{audit.isPending ? 'Auditing…' : 'Audit against the databases'}</button>}
        {operator && <button type="button" className="btn primary" disabled={coverage.isPending} onClick={() => coverage.mutate()}>{coverage.isPending ? 'Checking…' : 'Check coverage'}</button>}
      </PageHead>

      {[coverage.error, audit.error, dryRun.error, references.error].filter(Boolean).map((error, index) => <Failure key={index} error={error} />)}
      {dryRun.data && (
        <div className={`banner ${dryRun.data.problems.length ? 'bad' : 'ok'}`} role="status">
          {dryRun.data.problems.length ? (
            <div><strong>The dry run found {plural(dryRun.data.problems.length, 'problem')}:</strong>
              <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>{dryRun.data.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul></div>
          ) : <span>Dry run passed: every connection answers, every target is there and every policy covers its query. No rows moved.</span>}
        </div>
      )}

      {proof.isPending ? <Loading /> : proof.error ? <Failure error={proof.error} /> : <ProofBody data={proof.data} operator={operator} />}
    </>
  )
}

function ProofBody({ data, operator }: { data: ProofData; operator: boolean }) {
  const report = data.audit.report
  const coverage = data.coverages.length ? data.coverages : null
  const tables = (coverage ?? []).reduce((total, check) => total + check.report.tables.length, 0)
  const accounted = tables - (coverage ?? []).reduce((total, check) => total + (check.report.summary.uncovered ?? 0), 0)
  const oldest = (coverage ?? []).map((check) => check.at).sort()[0]
  const manifest = MANIFEST_STATES[data.manifest.state]

  return (
    <>
      <div className="stats">
        <div className="card">
          <div className="label">Coverage{coverage ? <> of {coverage.map((check) => check.report.database).join(', ')}</> : null}</div>
          {coverage ? (
            <>
              <div className="big">{accounted} of {plural(tables, 'table')}</div>
              <div className="meter" role="meter" aria-valuenow={accounted} aria-valuemin={0} aria-valuemax={tables} aria-label="Tables accounted for">
                <span style={{ width: `${(accounted / Math.max(1, tables)) * 100}%` }} />
              </div>
              <div className="small">Checked {ago(oldest)}{coverage.length < data.sources.length ? ` · ${data.sources.length - coverage.length} source(s) not checked` : ''}</div>
            </>
          ) : <><div className="big muted">Not checked</div><div className="small">{operator ? 'Check coverage reads the source\'s catalog, never its rows.' : 'An operator can check it.'}</div></>}
        </div>
        <div className="card">
          <div className="label">Audit of masking policies</div>
          <div className={`big ${report.summary.error ? 'bad' : ''}`}>{plural(report.summary.error, 'error')} · {plural(report.summary.warning, 'warning')}</div>
          <div className="small">{data.audit.connected ? `Against the databases, ${ago(data.audit.at)} by ${data.audit.by}` : 'From the configuration alone: columns judged by name'}</div>
        </div>
        <div className="card">
          <div className="label">Manifest of the last run</div>
          <div className={`big ${manifest.kind === 'bad' ? 'bad' : ''}`}>{manifest.label}</div>
          <div className="small">{manifest.hint}{data.manifest.signingKeyFingerprint ? <> Key <code>{data.manifest.signingKeyFingerprint}</code>.</> : null}</div>
        </div>
      </div>

      {coverage && coverage.map((check) => <CoverageTables key={check.id} report={check.report} at={check.at} by={check.by} />)}
      <References check={data.references} />
      <ScheduleCard />

      <section>
        <h2>Audit findings</h2>
        <div className="tablebox">
          <table>
            <thead><tr><th>Severity</th><th>Job</th><th>Finding</th></tr></thead>
            <tbody>
              {report.findings.map((finding, index) => (
                <tr key={index}>
                  <td><Pill kind={finding.severity === 'error' ? 'bad' : finding.severity === 'warning' ? 'warn' : 'idle'}>{finding.severity === 'info' ? 'Note' : finding.severity === 'error' ? 'Error' : 'Warning'}</Pill></td>
                  <td>{finding.job ? <Link className="mono" to={`/jobs/${finding.job}`}>{finding.job}</Link> : <span className="muted">All jobs</span>}</td>
                  <td className="wrap"><Prose text={finding.message} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!report.findings.length && <div className="empty">Nothing to report.</div>}
        </div>
      </section>

      <section>
        <h2>Last manifest</h2>
        {data.manifest.jobs ? (
          <>
            <p className="small" style={{ marginBottom: 8 }}>Written {when(data.manifest.generatedAt)} by {data.manifest.tool?.name} {data.manifest.tool?.version}, masked by {data.manifest.maskedBy} · <span className="mono">{data.manifest.where}</span></p>
            <div className="tablebox">
              <table>
                <thead><tr><th>Job</th><th>Table</th><th className="num">Rows</th><th className="num">Columns masked</th><th>Key</th><th>Status</th></tr></thead>
                <tbody>
                  {data.manifest.jobs.map((job) => (
                    <tr key={job.job}>
                      <td><Link className="mono" to={`/jobs/${job.job}`}>{job.job}</Link></td>
                      <td className="mono">{job.targetTable}</td>
                      <td className="num mono">{count(job.rowCount)}</td>
                      <td className="num">{job.columns}</td>
                      <td className="mono">{job.keyFingerprint}</td>
                      <td><JobStatus status={job.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : <p className="muted">{manifest.hint}</p>}
      </section>

      {data.manifests.length > 0 && (
        <section>
          <h2>Every manifest kept</h2>
          <p className="small" style={{ marginBottom: 8 }}>Kept by run as each appeared, since the manifest file is replaced by every run. To keep every one whatever happens, keep manifests in a table.</p>
          <div className="tablebox">
            <table>
              <thead><tr><th>Run</th><th>Written</th><th className="num">Jobs</th><th>Verifies</th><th><span className="visually-hidden">Evidence</span></th></tr></thead>
              <tbody>
                {data.manifests.map((kept) => (
                  <tr key={kept.key}>
                    <td className="mono">{kept.key.startsWith('sha256:') ? <span className="muted">before run ids</span> : <Link to={`/runs/${kept.key}`}>{kept.key.slice(0, 8)}</Link>}</td>
                    <td>{when(kept.generatedAt)}</td>
                    <td className="num">{kept.jobs}</td>
                    <td>{kept.verification.verified
                      ? <Pill kind="ok">{kept.verification.signed ? (kept.verification.signatureChecked ? 'Verified, signed' : 'Intact, signed') : 'Intact'}</Pill>
                      : <Pill kind="bad">Does not verify</Pill>}</td>
                    <td>{!kept.key.startsWith('sha256:') && <a href={`/api/runs/${kept.key}/evidence`} download>Evidence</a>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  )
}

function CoverageTables({ report, at, by }: { report: CoverageReport; at: string; by: string }) {
  return (
    <section>
      <h2>Tables in <span className="mono">{report.database}</span> <span className="small" style={{ fontWeight: 400 }}>checked {ago(at)} by {by}</span></h2>
      {report.acknowledgedButAbsent.length > 0 && (
        <div className="banner warn" style={{ marginBottom: 12 }}>Declared in <code>acknowledged</code> but no longer in the database: {report.acknowledgedButAbsent.join(', ')}.</div>
      )}
      <div className="tablebox">
        <table>
          <thead><tr><th>Table</th><th>State</th><th>Jobs, or why not copied</th></tr></thead>
          <tbody>
            {COVERAGE_STATES.flatMap(([state, label, kind]) => report.tables.filter((table) => table.state === state).map((table) => (
              <tr key={table.table}>
                <td className="mono">{table.table}</td>
                <td><Pill kind={kind}>{label}</Pill></td>
                <td className="wrap">
                  {state === 'uncovered' ? (table.personalDataColumns?.length ? <>Looks personal: <span className="mono">{table.personalDataColumns.join(', ')}</span></> : <span className="muted">Add a job, or declare it in acknowledged</span>)
                    : state === 'acknowledged' ? table.reason
                      : table.jobs?.map((job) => <Link key={job} className="mono" to={`/jobs/${job}`} style={{ marginRight: 8 }}>{job}</Link>)}
                </td>
              </tr>
            )))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function References({ check }: { check: Check<ReferencesReport> | null }) {
  if (!check) return null
  const report = check.report
  return (
    <section>
      <h2>References in the copies <span className="small" style={{ fontWeight: 400 }}>checked {ago(check.at)} by {check.by}</span></h2>
      <p className="small" style={{ marginBottom: 8 }}>Rows in a copy whose foreign key points at a row the copy doesn't hold: the keys each target declares, and those of the sources copied into it.</p>
      {report.unchecked.map((note) => <div key={note.database} className={`banner ${note.status === 'error' ? 'bad' : 'warn'}`} style={{ marginBottom: 8 }}><Prose text={note.message} /></div>)}
      <div className="tablebox">
        <table>
          <thead><tr><th>Copy</th><th>Table</th><th>References</th><th>Key</th><th className="num">Orphaned rows</th></tr></thead>
          <tbody>
            {report.references.map((reference) => (
              <tr key={`${reference.database}.${reference.table}.${reference.columns.join()}`}>
                <td className="mono">{reference.database}</td>
                <td className="mono">{reference.table}({reference.columns.join(', ')})</td>
                <td className="mono">{reference.referencedTable}({reference.referencedColumns.join(', ')})</td>
                <td>{reference.declared ? 'declared' : <span className="small">the source's</span>}</td>
                <td className="num">{reference.orphans === null ? <Pill kind="warn">Not checked</Pill> : reference.orphans ? <Pill kind="bad">{count(reference.orphans)}</Pill> : <Pill kind="ok">0</Pill>}
                  {reference.problem && <div className="small error">{reference.problem}</div>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!report.references.length && <div className="empty">No foreign key applies to these copies.</div>}
      </div>
    </section>
  )
}

const KIND_LABELS: Record<string, string> = { coverage: 'Coverage', audit: 'Audit against the databases', discovery: 'Discovery', references: 'References' }

function ScheduleCard() {
  const user = useUser()
  const client = useQueryClient()
  const current = useQuery({ queryKey: ['check-schedule'], queryFn: () => api<{ schedule: CheckSchedule; kinds: string[] }>('/checks/schedule') })
  const [draft, setDraft] = useState<CheckSchedule | null>(null)
  useEffect(() => { if (current.data) setDraft(current.data.schedule) }, [current.data])
  const save = useMutation({
    mutationFn: () => api<CheckSchedule>('/checks/schedule', { method: 'PUT', body: draft }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['check-schedule'] }),
  })
  if (!current.data || !draft) return null
  const admin = can(user, 'admin')
  const summary = current.data.schedule.enabled
    ? `Every day at ${current.data.schedule.time} UTC: ${current.data.schedule.kinds.map((kind) => KIND_LABELS[kind].toLowerCase()).join(', ')}.`
    : 'Off: checks run only when someone asks.'

  return (
    <form className="card stack" onSubmit={(event) => { event.preventDefault(); save.mutate() }}>
      <h2 style={{ margin: 0 }}>Nightly checks</h2>
      <p className="muted">{summary} What a check finds that the one before didn't is shown on the Overview.</p>
      {admin && (
        <>
          <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />Run them every day</label>
          <div className="row" style={{ alignItems: 'flex-end' }}>
            <label className="field">At, UTC<input type="time" value={draft.time} onChange={(event) => setDraft({ ...draft, time: event.target.value })} /></label>
            {current.data.kinds.map((kind) => (
              <label className="check" key={kind}>
                <input type="checkbox" checked={draft.kinds.includes(kind)}
                  onChange={() => setDraft({ ...draft, kinds: draft.kinds.includes(kind) ? draft.kinds.filter((each) => each !== kind) : [...draft.kinds, kind] })} />
                {KIND_LABELS[kind]}
              </label>
            ))}
          </div>
          {save.error && <p className="error" role="alert">{save.error.message}</p>}
          <div className="row"><button type="submit" className="btn primary" disabled={save.isPending}>Save</button>{save.isSuccess && <span className="small" role="status">Saved.</span>}</div>
        </>
      )}
    </form>
  )
}
