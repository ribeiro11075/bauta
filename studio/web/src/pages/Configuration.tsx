import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api, can } from '../api'
import { useUser } from '../App'
import { DiffView, Failure, Loading, PageHead } from '../components/ui'

interface ConfigurationData {
  valid: boolean
  problem?: string
  problems?: string[]
  files: { path: string; jobs: string[] }[]
  directory?: string
}

interface OpenFile {
  path: string
  text: string
  digest: string
}

export function Configuration() {
  const user = useUser()
  const configuration = useQuery({ queryKey: ['configuration'], queryFn: () => api<ConfigurationData>('/configuration') })
  const [params, setParams] = useSearchParams()
  const files = configuration.data?.files ?? []
  const chosen = params.get('file') ?? files[0]?.path ?? null

  return (
    <>
      <PageHead title="Configuration" sub={<>The jobs files in <code>{configuration.data?.directory ?? 'the configuration directory'}</code>, edited in place. A change is saved only if the whole configuration still validates; the file as it was is kept, and the change recorded with who made it.</>} />
      {configuration.isPending ? <Loading /> : configuration.error ? <Failure error={configuration.error} /> : (
        <>
          {!configuration.data.valid && (
            <div className="banner bad" role="alert"><div><strong>The configuration doesn't load.</strong><pre className="error mono" style={{ margin: '8px 0 0' }}>{configuration.data.problem}</pre>
              <p style={{ marginTop: 8 }}>Studio can't open the files until it loads. Fix it on the server, where it's kept.</p></div></div>
          )}
          {configuration.data.problems && configuration.data.problems.length > 0 && (
            <div className="banner warn" role="status"><div><strong>Loads, but validate finds:</strong><ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>{configuration.data.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul></div></div>
          )}
          {files.length > 0 && (
            <div className="row" role="tablist" aria-label="Jobs files">
              {files.map((file) => (
                <button key={file.path} type="button" role="tab" aria-selected={file.path === chosen} className={`btn sm ${file.path === chosen ? 'primary' : ''}`}
                  onClick={() => setParams({ file: file.path })}>{file.path} <span className="small" style={{ color: 'inherit' }}>· {file.jobs.length} jobs</span></button>
              ))}
            </div>
          )}
          {chosen && (can(user, 'admin') ? <Editor key={chosen} path={chosen} /> : <p className="muted">Only an admin can open and edit the jobs files.</p>)}
        </>
      )}
    </>
  )
}

function Editor({ path }: { path: string }) {
  const client = useQueryClient()
  const file = useQuery({ queryKey: ['file', path], queryFn: () => api<OpenFile>('/configuration/file', { query: { path } }), refetchOnWindowFocus: false })
  const [text, setText] = useState('')
  const [saved, setSaved] = useState<string | null>(null)
  useEffect(() => { if (file.data) setText(file.data.text) }, [file.data])

  const save = useMutation({
    mutationFn: () => api<{ digest: string; diff: string; backup: string }>('/configuration/file', { method: 'PUT', query: { path }, body: { text, digest: file.data!.digest } }),
    onSuccess: (written) => {
      setSaved(written.diff)
      client.setQueryData(['file', path], { path, text, digest: written.digest })
      client.invalidateQueries({ predicate: (query) => query.queryKey[0] !== 'file' })
    },
  })

  if (file.isPending) return <Loading />
  if (file.error) return <Failure error={file.error} />
  const changed = text !== file.data.text

  return (
    <section className="stack">
      <label className="visually-hidden" htmlFor="editor">{path}</label>
      <textarea id="editor" className="code" spellCheck={false} value={text} onChange={(event) => { setText(event.target.value); setSaved(null) }} />
      {save.error && <div className="banner bad" role="alert"><pre className="error mono" style={{ margin: 0 }}>{save.error.message}</pre></div>}
      {saved !== null && !changed && (
        <div className="banner ok" role="status"><div><strong>Saved.</strong> The next run uses it.
          <DiffView diff={saved} /></div></div>
      )}
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button type="button" className="btn" disabled={!changed} onClick={() => { setText(file.data.text); save.reset() }}>Discard changes</button>
        <button type="button" className="btn primary" disabled={!changed || save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Validating…' : 'Validate and save'}</button>
      </div>
    </section>
  )
}
