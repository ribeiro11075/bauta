import { describe, expect, it, vi } from 'vitest'
import { api, ApiError, can, type User } from './api'

function answering(status: number, body: unknown) {
  const fetchMock = vi.fn(async (..._: unknown[]) => new Response(body === undefined ? '' : JSON.stringify(body), { status }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('the API client', () => {
  it('sends the header without which the server refuses any change', async () => {
    const fetchMock = answering(200, {})
    await api('/runs', { method: 'POST', body: { jobs: [] } })
    const [url, options] = fetchMock.mock.calls[0] as [URL, RequestInit]
    expect(url.pathname).toBe('/api/runs')
    expect(options.headers).toMatchObject({ 'X-Bauta-Studio': '1', 'Content-Type': 'application/json' })
    expect(options.body).toBe('{"jobs":[]}')
  })

  it('puts a query in the URL', async () => {
    const fetchMock = answering(200, {})
    await api('/configuration/file', { query: { path: '../shared/extra.yaml' } })
    expect((fetchMock.mock.calls[0] as [URL])[0].searchParams.get('path')).toBe('../shared/extra.yaml')
  })

  it('throws what the server said went wrong', async () => {
    answering(409, { detail: 'copyPeople is already being measured' })
    const error = await api('/jobs/copyPeople/bench', { method: 'POST', body: {} }).catch((caught: ApiError) => caught) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(409)
    expect(error.message).toBe('copyPeople is already being measured')
  })

  it('says why the configuration does not load', async () => {
    answering(409, { detail: { configuration: 'jobs.yaml is not valid YAML' } })
    await expect(api('/jobs')).rejects.toThrow('jobs.yaml is not valid YAML')
  })

  it('takes an empty answer as nothing to read', async () => {
    answering(200, undefined)
    await expect(api('/auth/logout', { method: 'POST' })).resolves.toEqual({})
  })
})

describe('roles', () => {
  const user = (role: User['role']) => ({ role }) as User

  it('lets each role do what those below it can', () => {
    expect(can(user('admin'), 'viewer')).toBe(true)
    expect(can(user('operator'), 'operator')).toBe(true)
    expect(can(user('viewer'), 'operator')).toBe(false)
  })
})
