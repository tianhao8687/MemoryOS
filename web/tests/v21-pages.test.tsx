import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'

const health = {
  memory_id: 'memory-cold-1',
  title: 'Old cache failure',
  memory_status: 'active',
  temperature: 'cold',
  health_score: 0.31,
  components: { recency: 0.1, usage: 0.2 },
  explanation: 'Cold at 0.310; usage and recency are low.',
  retrieval_count: 1,
  last_retrieved_at: null,
  archived_at: null,
  evaluated_at: '2026-08-10T10:00:00Z',
}

function body(path: string) {
  if (path.startsWith('/api/status')) return { version: '2.2.0', counts: {} }
  if (path.startsWith('/api/repositories')) return []
  if (path.startsWith('/api/memory-health/evaluate')) return { ok: true, evaluated: 1, counts: { cold: 1 } }
  if (path.startsWith('/api/memory-health')) return [health]
  if (path.startsWith('/api/possible-conflicts'))
    return [{ id: 'possible-1', left_claim_id: 'claim-left', right_claim_id: 'claim-right', status: 'abstained', deterministic_relationship: 'uncertain', deterministic_confidence: 0.5, reason: 'Rules inconclusive', model_result: { relationship: 'uncertain', confidence: 0.2, explanation: 'Insufficient evidence', abstain: true }, provider_fingerprint: 'fixture:model:abc', prompt_version: 'relationship-judge-v2.1.0', evidence_hash: 'a'.repeat(64), created_at: '2026-08-10T10:00:00Z', resolved_at: null, resolved_by: null }]
  return []
}

function renderRoute(route: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}><App /></MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('V2.1 reality intelligence pages', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      const path = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      return Promise.resolve(new Response(JSON.stringify(body(path)), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('renders explainable health and keyboard-accessible lifecycle controls', async () => {
    const user = userEvent.setup()
    renderRoute('/memory-health')
    expect(await screen.findByRole('heading', { name: 'Memory Health' })).toBeInTheDocument()
    expect(await screen.findByText('Old cache failure')).toBeInTheDocument()
    expect(screen.getByText(/Cold at 0.310/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'cold 1' }))
    expect(screen.getByRole('button', { name: 'cold 1' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Archive' })).toBeEnabled()
  })

  it('shows abstention evidence without presenting it as accepted truth', async () => {
    renderRoute('/possible-conflicts')
    expect(await screen.findByRole('heading', { name: 'Possible Conflicts' })).toBeInTheDocument()
    expect(await screen.findByText('Insufficient evidence')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Confirm conflict' })).toBeEnabled()
  })
})
