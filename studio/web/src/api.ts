// The JSON API, and the shapes it returns. Every request carries Studio's
// header, without which the server refuses any change (see auth.py).

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : status === 409 && isConfigurationProblem(detail) ? detail.configuration : `request failed (${status})`)
    this.status = status
    this.detail = detail
  }
}

export function isConfigurationProblem(detail: unknown): detail is { configuration: string } {
  return typeof detail === 'object' && detail !== null && 'configuration' in detail
}

export async function api<T>(path: string, options: { method?: string; body?: unknown; query?: Record<string, string> } = {}): Promise<T> {
  const url = new URL('/api' + path, window.location.origin)
  for (const [name, value] of Object.entries(options.query ?? {})) url.searchParams.set(name, value)
  const response = await fetch(url, {
    method: options.method ?? 'GET',
    credentials: 'same-origin',
    headers: { 'X-Bauta-Studio': '1', ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  })
  const text = await response.text()
  const data = text ? JSON.parse(text) : {}
  if (!response.ok) throw new ApiError(response.status, data.detail)
  return data as T
}

export type Role = 'viewer' | 'operator' | 'admin'
export const ROLES: Role[] = ['viewer', 'operator', 'admin']

export interface User {
  id: number
  email: string
  name: string
  role: Role
  active: boolean
  createdAt: string | null
  lastLoginAt: string | null
}

export function can(user: User, role: Role): boolean {
  return ROLES.indexOf(user.role) >= ROLES.indexOf(role)
}

export type Stages = { read: number; mask: number; write: number; wait: number }

export interface Run {
  runId: string
  status: 'running' | 'succeeded' | 'failed' | 'partial' | 'stopped'
  startedAt: string | null
  finishedAt: string | null
  jobCount: number
  counts: Record<string, number>
  rowCount: number
  durationSeconds: number | null
  stages: Stages | null
  live: boolean
  host: string | null
  pid: number | null
  startedBy?: string | null
}

export interface JobRecord {
  job: string
  status: string
  rowCount: number | null
  startedAt: string | null
  finishedAt: string | null
  durationSeconds?: number
  error: string | null
  attempts?: number
  readSeconds?: number | null
  maskSeconds?: number | null
  writeSeconds?: number | null
  throttledSeconds?: number | null
  runId?: string
}

export interface Launched {
  id: number
  email: string
  startedAt: string
  finishedAt: string | null
  jobs: string[]
  options: { fullRefresh?: boolean }
  pid: number | null
  exitCode: number | null
  runId: string | null
  log?: string | null
}

export interface RunDetail extends Run {
  jobs: JobRecord[]
  launched?: Launched
  log?: string | null
  cancelledBy?: string | null
  hasManifest?: boolean
}

export interface Job {
  name: string
  active: boolean
  sourceConnection: string
  sourceQuery: string
  targetConnection: string
  targetTable: string
  stageTable: string | null
  insertStrategy: string
  watermarkColumn: string | null
  partitions: string | null
  predecessors: string[]
  refresh: string | null
  unmasked: boolean
  masking: { keyFingerprint: string; columns: Record<string, string>; defaultStrategy: string | null } | null
  file: string | null
  lastRun?: JobRecord | null
  history?: JobRecord[]
  bench?: import('./components/Bench').BenchCheck | null
}

export interface Finding {
  severity: 'error' | 'warning' | 'info'
  job: string | null
  message: string
}

export interface AuditReport {
  summary: { error: number; warning: number; info: number }
  findings: Finding[]
  jobs: { job: string; masked: boolean; unmasked: boolean; active: boolean }[]
  generatedAt: string
}

export interface CoverageReport {
  database: string
  summary: Record<string, number>
  tables: { table: string; state: string; jobs?: string[]; reason?: string; personalDataColumns?: string[] }[]
  acknowledgedButAbsent: string[]
}

export interface ManifestState {
  state: 'not-kept' | 'not-written' | 'unreadable' | 'altered' | 'intact-unsigned' | 'unsigned-with-key' | 'signed-unchecked' | 'verified' | 'signature-invalid'
  where?: string
  problem?: string
  generatedAt?: string
  maskedBy?: string
  tool?: { name: string; version: string }
  signingKeyFingerprint?: string | null
  jobs?: { job: string; status: string; rowCount: number; keyFingerprint: string; targetTable: string; columns: number }[]
}

export interface Check<T> {
  id: number
  kind: string
  at: string
  by: string
  report: T
}

export interface ReferencesReport {
  generatedAt: string
  references: { database: string; table: string; columns: string[]; referencedTable: string; referencedColumns: string[]; declared: boolean;
    orphans: number | null; problem: string | null; status: string }[]
  unchecked: { database: string; status: string; message: string }[]
  summary: { checked: number; orphaned: number; notChecked: number }
}

export interface CheckSchedule {
  enabled: boolean
  time: string
  kinds: string[]
}

export interface News {
  kind: 'coverage' | 'discovery' | 'references'
  at: string
  by: string
  database?: string
  table?: string
  personalDataColumns?: string[]
  job?: string
  columns?: string[]
  reference?: string
  orphans?: number
}

export interface Connection {
  alias: string
  type: string
  target: string
  readOnly: boolean
  requireMasking: boolean
  requireEncryption: boolean
  sourceOf: string[]
  targetOf: string[]
}

export interface Activity {
  id: number
  at: string
  email: string
  action: string
  target: string
  detail: Record<string, unknown>
}

export interface ScheduleEntry {
  job: string
  state: 'due' | 'throttled' | 'inactive'
  waitsFor: string[]
  refreshMinutes: number | null
  lastRunAt: string | null
  nextDueAt: string | null
  lastCompletedAt: string | null
  staleAfterHours: number
  ownThreshold: boolean
  stale: boolean
  neverCompleted: boolean
}

export interface Staleness {
  defaultHours: number
  jobs: Record<string, number>
}

export interface DriftedJob {
  job: string
  file: string | null
  added: { column: string; policy: Record<string, unknown>; reason: string }[]
  removed: string[]
}

export interface DiscoveryCheck {
  id: number
  at: string
  by: string
  jobs: DriftedJob[]
  checkedJobs: number
}

export interface FormColumn {
  column: string
  strategy: string | null
  custom: boolean
  summary?: string | null
}

export interface JobFormData {
  name: string
  file: string
  digest: string
  fields: Record<string, unknown>
  inherited: Record<string, unknown>
  masking: {
    enabled: boolean
    columns: FormColumn[]
    defaultStrategy: string | null
    customDefault: boolean
    key: { source: 'defaults' | 'variable' | 'file' | 'none'; variable?: string }
  }
  choices: { strategies: string[]; connections: string[]; jobs: string[]; insertStrategies: string[] }
  others: string[]
}

export interface Written {
  path: string
  digest: string
  diff: string
  backup: string
}
