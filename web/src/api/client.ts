import type {
  AuditEvent,
  ConflictRecord,
  ContextResponse,
  DoctorResponse,
  ExplainResponse,
  MemoryRecord,
  Repository,
  SearchResponse,
  StatusResponse,
} from '../types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
    ...init,
  })
  const body = (await response.json().catch(() => null)) as unknown
  if (!response.ok) {
    const envelope = body as {
      error?: { code?: string; message?: string; details?: Record<string, unknown> }
      detail?: string
    } | null
    throw new ApiError(
      response.status,
      envelope?.error?.code ?? 'HTTP_ERROR',
      envelope?.error?.message ?? envelope?.detail ?? `Request failed (${response.status})`,
      envelope?.error?.details,
    )
  }
  return body as T
}

function queryString(values: Record<string, string | number | boolean | null | undefined>): string {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') params.set(key, String(value))
  })
  return params.toString()
}

export const api = {
  status: () => request<StatusResponse>('/api/status'),
  doctor: () => request<DoctorResponse>('/api/doctor'),
  repositories: () => request<Repository[]>('/api/repositories'),
  detectRepository: (path: string) =>
    request<Repository>('/api/repositories/detect', {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),
  search: (values: Record<string, string | number | boolean | null | undefined>) =>
    request<SearchResponse>(`/api/memories?${queryString(values)}`),
  memory: (id: string) => request<MemoryRecord>(`/api/memories/${id}`),
  explain: (id: string) => request<ExplainResponse>(`/api/memories/${id}/explain`),
  history: (id: string) => request<MemoryRecord[]>(`/api/memories/${id}/history`),
  propose: (payload: Record<string, unknown>) =>
    request<{ ok: true; memory: MemoryRecord }>('/api/memories', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  update: (id: string, payload: Record<string, unknown>) =>
    request<{ ok: true; memory: MemoryRecord }>(`/api/memories/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  confirm: (id: string, strategy?: string, rationale?: string) =>
    request<{ ok: true; memory: MemoryRecord }>(`/api/memories/${id}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ strategy, rationale }),
    }),
  reject: (id: string) =>
    request<{ ok: true; memory: MemoryRecord }>(`/api/memories/${id}/reject`, {
      method: 'POST',
    }),
  forget: (id: string) =>
    request<{ ok: true; memory: MemoryRecord }>(`/api/memories/${id}/forget`, {
      method: 'POST',
    }),
  context: (repository: string, branch: string, task: string) =>
    request<ContextResponse>('/api/context', {
      method: 'POST',
      body: JSON.stringify({ repository, branch, task, budget: 6000 }),
    }),
  conflicts: () => request<ConflictRecord[]>('/api/conflicts'),
  resolveConflict: (id: string, strategy: string, rationale: string) =>
    request<{ ok: true; memory: MemoryRecord }>(`/api/conflicts/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ strategy, rationale }),
    }),
  timeline: (limit = 100) => request<AuditEvent[]>(`/api/timeline?limit=${limit}`),
  audit: (limit = 100) => request<AuditEvent[]>(`/api/audit?limit=${limit}`),
  backup: () => request<{ ok: true; path: string }>('/api/backup', { method: 'POST' }),
  exportData: () => request<{ ok: true; path: string }>('/api/export', { method: 'POST' }),
  settings: () =>
    request<{
      database_path: string
      backup_path: string
      mcp_status: string
      provider_status: string
      host: string
      telemetry: boolean
    }>('/api/settings'),
}
