import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'

const claim = {
  id: 'claim-11111111',
  memory_id: 'memory-11111111',
  subject: {
    id: 'entity-1',
    canonical_name: 'production database',
    normalized_name: 'production database',
    entity_type: 'database',
    aliases: ['prod db'],
  },
  predicate: 'uses',
  object_kind: 'literal',
  object_value: 'PostgreSQL',
  polarity: 'positive',
  modality: 'decision',
  confidence: 0.96,
  status: 'accepted',
  valid_from: null,
  valid_to: null,
  recorded_at: '2026-08-10T10:00:00Z',
  stale_state: 'fresh',
}

function bodyFor(path: string) {
  if (path.startsWith('/api/status'))
    return {
      version: '2.1.0',
      database: 'memoryos.db',
      schema_version: '0003_reality_intelligence_hardening',
      counts: { candidate: 0, active: 1, superseded: 0, expired: 0, forgotten: 0, rejected: 0 },
      sources: 1,
      provenance_rate: 1,
      conflicts: 0,
      embedding_provider: 'disabled',
      mode: 'offline',
    }
  if (path.startsWith('/api/repositories'))
    return [{ id: 'repo', stable_key: 'memoryos', name: 'memoryos', path: 'C:/memoryos', remote_url: null, default_branch: 'main' }]
  if (path.startsWith('/api/current-truth'))
    return {
      state: 'resolved',
      truths: [{ subject: claim.subject, predicate: 'uses', state: 'resolved', accepted_claims: [claim], conflicting_claims: [], evidence: [{ source: 'manual' }], freshness: ['fresh'], resolution_history: [] }],
      accepted_claims: [claim], conflicting_claims: [], evidence: [], freshness: ['fresh'], resolution_history: [], as_of_valid_time: '2026-08-10T10:00:00Z', as_known_at: '2026-08-10T10:00:00Z',
    }
  if (path.startsWith('/api/claim-graph'))
    return { state: 'resolved', nodes: [claim], edges: [{ id: 'edge-1', from: claim.id, to: 'claim-22222222', type: 'supports', confidence: 0.91, method: 'rule', explanation: 'Evidence values overlap.' }] }
  if (path.startsWith('/api/freshness'))
    return [{ anchor_id: 'anchor-1', memory_id: claim.memory_id, memory_title: 'Production database', claim_id: claim.id, path: 'memoryos/config.py', symbol_fqn: 'MemoryOSSettings', freshness: 'suspect', commit_sha: 'a'.repeat(40), cached_head: null, checked_at: '2026-08-10T10:00:00Z' }]
  if (path.startsWith('/api/consolidations'))
    return [{ id: 'consolidation-1', scope_type: 'repository', scope_key: 'memoryos', subject_entity_id: 'entity-1', predicate: 'uses', proposal: { object: 'PostgreSQL', confidence: 0.93 }, status: 'candidate', source_memory_ids: ['memory-1', 'memory-2', 'memory-3'], counterevidence: [], created_at: '2026-08-10T10:00:00Z' }]
  if (path.startsWith('/api/debug/context'))
    return { task: 'architecture', repository: 'memoryos', branch: 'main', budget: 6000, characters_used: 420, retrieval_mode: 'rrf-fts5', retrieval_run_id: 'run-11111111', query_plan: { intent: 'current_decision' }, truth_state: 'resolved', sections: {}, manifest: [{ memory_id: claim.memory_id, claim_ids: [claim.id], included: true, inclusion_reason: 'required coverage: decision', exclusion_reason: null, utility: 0.91, cost: 220, truth_state: 'resolved', freshness: 'fresh', retrieval_trace: { fts_rank: 1 } }], text: 'Project Memory Context\nUse PostgreSQL', debug: { config_hash: 'b'.repeat(64), reranker: 'disabled', candidates: [] } }
  if (path.startsWith('/api/benchmarks/memorybench-v2'))
    return { schema: 'memorybench-v2-report@1', generated_at: '2026-08-10T10:00:00Z', seed: 20260810, config_hash: 'c'.repeat(64), git: { commit: 'd'.repeat(40), dirty: true }, provider_policy: { default: 'heuristic/deterministic-rules-v2' }, suites: { retrieval: { suite: 'R Retrieval', sample_size: 250, evidence_type: 'synthetic-deterministic', baseline: { recall_at_5: 0.8 }, v2: { recall_at_5: 1 }, gate: { passed: true, rule: 'Recall@5' } }, agent_ab: { suite: 'A Agent A/B', sample_size: 30, fixture: { baseline: { task_success: 0.5 }, memoryos_enabled: { task_success: 0.8 } }, truthfulness_gate: { passed: true, reason: 'fixture only' } } }, release_gates: { measured_all_passed: true, real_model_agent_effect: 'external_blocker', release_readiness: 'conditional_external_blocker', note: 'All measured gates pass.' } }
  if (path.startsWith('/api/benchmarks/coding-memory-bench-v2.1'))
    return { schema: 'coding-memory-bench-v2.1@1', generated_at: '2026-08-10T10:00:00Z', blind_protocol: { runtime_payload_contains_gold: false, gold_loaded_only_by_scorer: true, immutable_input_hash: 'e'.repeat(64), immutable_gold_hash: 'f'.repeat(64) }, sample_sizes: { retrieval_hard_negatives: 100, temporal: 100, conflict: 100 }, modes: { baseline: { retrieval_recall_at_5: 0, temporal_accuracy: 0, conflict: { precision: 0.5, recall: 1, f1: 0.667 }, perfect_score_warning: null, real_model: false, model_status: 'not_applicable' }, v2: { retrieval_recall_at_5: 1, temporal_accuracy: 1, conflict: { precision: 1, recall: 1, f1: 1 }, perfect_score_warning: 'Perfect score detected; expand adversarial cases.', real_model: false, model_status: 'not_applicable' }, v2_model: { retrieval_recall_at_5: 1, temporal_accuracy: 1, conflict: { precision: 1, recall: 1, f1: 1 }, perfect_score_warning: 'Perfect score detected; expand adversarial cases.', real_model: false, model_status: 'external_blocker' } }, release_gates: { blind_gold_isolation: true }, all_measured_gates_passed: true, truthfulness: 'No model effect claim.' }
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

describe('Memory Intelligence Workbench', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      const path = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      return Promise.resolve(new Response(JSON.stringify(bodyFor(path)), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('renders current truth and a bounded claim graph', async () => {
    renderRoute('/current-truth')
    expect(await screen.findByRole('heading', { name: 'Current Truth' })).toBeInTheDocument()
    expect(await screen.findByText('PostgreSQL')).toBeInTheDocument()
    expect(screen.getAllByText('resolved')).toHaveLength(2)

    renderRoute('/claim-graph')
    expect(await screen.findByRole('heading', { name: 'Claim Graph' })).toBeInTheDocument()
    expect(await screen.findByText('Evidence values overlap.')).toBeInTheDocument()
  })

  it('renders freshness and consolidation evidence with actionable controls', async () => {
    renderRoute('/freshness')
    expect(await screen.findByRole('heading', { name: 'Git Freshness' })).toBeInTheDocument()
    expect(await screen.findByText('memoryos/config.py')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()

    renderRoute('/consolidation')
    expect(await screen.findByRole('heading', { name: 'Consolidation Inbox' })).toBeInTheDocument()
    expect(await screen.findByText('PostgreSQL')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Preview' })).toBeEnabled()
  })

  it('shows retrieval decisions and keeps fixture evidence separate on the dashboard', async () => {
    const user = userEvent.setup()
    renderRoute('/retrieval-debugger')
    await user.click(await screen.findByRole('button', { name: 'Run trace' }))
    expect(await screen.findByText('required coverage: decision')).toBeInTheDocument()
    expect(screen.getByText('rrf-fts5')).toBeInTheDocument()

    renderRoute('/benchmarks')
    expect(await screen.findByRole('heading', { name: 'Benchmark Dashboard' })).toBeInTheDocument()
    expect(await screen.findByText('R Retrieval')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Blind CodingMemoryBench V2.1' })).toBeInTheDocument()
    expect(screen.getByText('Gold isolated')).toBeInTheDocument()
    expect(screen.getByText('Real-model Agent A/B: external blocker')).toBeInTheDocument()
    expect(screen.getByText('Fixture only')).toBeInTheDocument()
  })
})
