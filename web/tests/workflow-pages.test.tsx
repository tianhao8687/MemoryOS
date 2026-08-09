import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'

const active = {
  id: '11111111-1111-1111-1111-111111111111',
  scope_type: 'repository',
  scope_key: 'memoryos',
  memory_type: 'project',
  category: 'decision',
  subject: null,
  key: 'architecture.backend.framework',
  title: 'Backend framework: FastAPI',
  content: 'Use FastAPI for the backend.',
  status: 'active',
  confidence: 0.92,
  importance: 0.82,
  valid_from: null,
  valid_to: null,
  ttl_seconds: null,
  supersedes_id: null,
  created_at: '2026-08-09T10:00:00Z',
  updated_at: '2026-08-09T10:00:00Z',
  created_by: 'manual',
  sensitivity: 'normal',
  metadata: {},
} as const

const candidate = {
  ...active,
  id: '22222222-2222-2222-2222-222222222222',
  title: 'Backend framework: Django',
  content: 'Use Django for the backend.',
  status: 'candidate',
  confidence: 0.81,
  created_by: 'agent',
} as const

const status = {
  version: '1.0.0',
  database: 'memoryos.db',
  schema_version: '0001_initial',
  counts: {
    candidate: 1,
    active: 1,
    superseded: 0,
    expired: 0,
    forgotten: 0,
    rejected: 0,
  },
  sources: 2,
  provenance_rate: 1,
  conflicts: 1,
  embedding_provider: 'disabled',
  mode: 'offline',
}

const explanation = {
  memory: candidate,
  sources: [
    {
      id: 'source-1',
      source_type: 'agent',
      source_ref: 'agent:unit-test',
      captured_at: '2026-08-09T10:00:00Z',
      excerpt: 'Use Django for the backend.',
      content_hash: 'a'.repeat(64),
      metadata: {},
    },
  ],
  relations: [],
  audit: [
    {
      id: 'audit-1',
      action: 'propose',
      entity_type: 'memory',
      entity_id: candidate.id,
      actor: 'agent',
      timestamp: '2026-08-09T10:00:00Z',
      details: {},
    },
  ],
  reason: 'Source-backed candidate',
}

function bodyFor(path: string) {
  if (path.startsWith('/api/status')) return status
  if (path.startsWith('/api/repositories'))
    return [
      {
        id: 'repo',
        stable_key: 'memoryos',
        name: 'memoryos',
        path: 'C:/memoryos',
        remote_url: null,
        default_branch: 'main',
      },
    ]
  if (path.includes('/explain')) return explanation
  if (path.startsWith('/api/memories'))
    return { items: [{ memory: candidate, score: 1, lexical_score: 1 }], total: 1, mode: 'fts5' }
  if (path.startsWith('/api/conflicts'))
    return [
      {
        candidate,
        current: [active],
        semantic_key: 'architecture.backend.framework',
        status: 'needs_review',
      },
    ]
  if (path.startsWith('/api/timeline'))
    return [
      {
        id: 'audit-1',
        action: 'create_active',
        entity_type: 'memory',
        entity_id: active.id,
        actor: 'manual',
        timestamp: '2026-08-09T10:00:00Z',
        details: { source: 'manual' },
      },
    ]
  return []
}

function renderRoute(route: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('workflow pages', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      const path =
        typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      return Promise.resolve(
        new Response(JSON.stringify(bodyFor(path)), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })
  })

  afterEach(() => vi.unstubAllGlobals())

  it('renders candidate review controls with explain provenance', async () => {
    renderRoute('/candidates')
    expect(await screen.findByRole('heading', { name: 'Candidates' })).toBeInTheDocument()
    expect(
      await screen.findByRole('complementary', { name: /Review candidate:/ }),
    ).toBeInTheDocument()
    expect(await screen.findByText('Why this memory?')).toBeInTheDocument()
    expect(screen.getByText('agent:unit-test')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Confirm$/ })).toBeInTheDocument()
  })

  it('renders old and new values with guarded conflict resolution', async () => {
    renderRoute('/conflicts')
    expect(await screen.findByRole('heading', { name: 'Conflicts' })).toBeInTheDocument()
    expect(
      await screen.findByRole('heading', { name: 'Backend framework: FastAPI' }),
    ).toBeInTheDocument()
    expect(
      await screen.findAllByRole('heading', { name: 'Backend framework: Django' }),
    ).toHaveLength(2)
    expect(await screen.findByRole('button', { name: 'Confirm resolution' })).toBeDisabled()
  })

  it('renders audit events as a timeline', async () => {
    renderRoute('/timeline')
    expect(await screen.findByRole('heading', { name: 'Timeline' })).toBeInTheDocument()
    expect(await screen.findByText('create active')).toBeInTheDocument()
    expect(screen.getByText('manual')).toBeInTheDocument()
  })
})
