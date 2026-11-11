import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { User } from '../api'
import { BenchCard, type BenchCheck } from './Bench'

let role: User['role'] = 'operator'
vi.mock('../App', () => ({ useUser: () => ({ role }) }))

const bench = (status: BenchCheck['status'], extra: Partial<BenchCheck> = {}): BenchCheck =>
  ({ id: 1, at: '2026-10-16T12:00:00Z', by: 'ann@example.com', job: 'copyPeople', rows: 1000, status, ...extra })

function show(check: BenchCheck | null) {
  render(<QueryClientProvider client={new QueryClient()}><BenchCard job="copyPeople" bench={check} /></QueryClientProvider>)
}

afterEach(() => { cleanup(); role = 'operator' })

describe('measuring a job', () => {
  it('can be started when nothing is running', () => {
    show(null)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Measure' }).disabled).toBe(false)
  })

  it('cannot be started again while one runs', () => {
    show(bench('running'))
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Measure' }).disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toContain('Measuring')
  })

  it('can be started again once a restart has ended one, and says so', () => {
    show(bench('failed', { problem: 'Studio restarted while this ran' }))
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Measure' }).disabled).toBe(false)
    expect(screen.getByRole('alert').textContent).toContain('Studio restarted')
  })

  it('is not offered to a viewer', () => {
    role = 'viewer'
    show(null)
    expect(screen.queryByRole('button', { name: 'Measure' })).toBeNull()
  })
})
