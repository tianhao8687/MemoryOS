import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'

const activeMemory = {
  id: '11111111-1111-1111-1111-111111111111', scope_type: 'repository', scope_key: 'memoryos', memory_type: 'project', category: 'decision', subject: null, key: 'architecture.backend', title: 'Use FastAPI', content: 'Use FastAPI for the backend.', status: 'active', confidence: 0.92, importance: 0.8, valid_from: null, valid_to: null, ttl_seconds: null, supersedes_id: null, created_at: '2026-08-09T10:00:00Z', updated_at: '2026-08-09T10:00:00Z', created_by: 'manual', sensitivity: 'normal', metadata: {},
}

function response(path: string) {
  if (path.startsWith('/api/status')) return { version: '2.2.0', database: 'memoryos.db', schema_version: '0003_reality_intelligence_hardening', counts: { candidate: 1, active: 1, superseded: 0, expired: 0, forgotten: 0, rejected: 0 }, sources: 2, provenance_rate: 1, conflicts: 0, embedding_provider: 'disabled', mode: 'offline' }
  if (path.startsWith('/api/repositories')) return [{ id: 'repo', stable_key: 'memoryos', name: 'memoryos', path: 'C:/memoryos', remote_url: null, default_branch: 'main' }]
  if (path.startsWith('/api/conflicts')) return []
  if (path.startsWith('/api/timeline')) return []
  if (path.startsWith('/api/context')) return { task: 'project', repository: 'memoryos', branch: 'main', budget: 6000, characters_used: 50, retrieval_mode: 'fts5', sections: { 'CURRENT DECISIONS': [activeMemory] }, text: 'Use FastAPI' }
  if (path.startsWith('/api/memories')) return { items: [{ memory: activeMemory, score: 1, lexical_score: 1 }], total: 1, mode: 'fts5' }
  return {}
}

function renderApp(initial = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}><App /></MemoryRouter></QueryClientProvider>)
}

describe('MemoryOS app shell', () => {
  beforeEach(() => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      return Promise.resolve(
        new Response(JSON.stringify(response(path)), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('renders the eight required navigation destinations and project context', async () => {
    renderApp()
    expect(await screen.findByRole('heading', { name: 'Project memory' })).toBeInTheDocument()
    expect(await screen.findByText('Use FastAPI')).toBeInTheDocument()
    for (const label of ['Overview', 'Projects', 'Memories', 'Candidates', 'Timeline', 'Conflicts', 'Settings', 'Audit']) expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
  })

  it('navigates by keyboard-accessible links to the memories table', async () => {
    const user = userEvent.setup()
    renderApp()
    await user.click(screen.getByRole('link', { name: 'Memories' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Memories' })).toBeInTheDocument())
    expect(screen.getByRole('searchbox', { name: 'Search all memory' })).toBeInTheDocument()
  })

  it('offers explicit manual activation and a TTL control', async () => {
    const user = userEvent.setup()
    renderApp()
    await user.click(screen.getByRole('button', { name: 'Add memory' }))
    expect(screen.getByRole('checkbox', { name: /Activate immediately/ })).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: 'TTL seconds (optional)' })).toBeInTheDocument()
  })
})
