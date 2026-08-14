import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, BarChart3, CheckCircle2, Cpu, FlaskConical } from 'lucide-react'
import { api } from '../api/client'
import { ErrorState, LoadingState } from '../components/AsyncState'
import { formatTime } from '../lib/format'
import type { MemoryBenchSuite } from '../types'

const metricPriority = [
  'recall_at_5',
  'f1',
  'accuracy',
  'stale_recall',
  'selected_precision',
  'redundancy_rate',
  'branch_leakage',
  'p95_ms',
  'task_success',
  'repeated_mistake_rate',
]

function metric(summary: Record<string, unknown> | undefined): string {
  if (!summary) return '—'
  for (const key of metricPriority) {
    const value = summary[key]
    if (typeof value === 'number') {
      const formatted = key.endsWith('_ms') ? `${value.toFixed(2)} ms` : value.toFixed(3)
      return `${key.replaceAll('_', ' ')} · ${formatted}`
    }
  }
  return 'Recorded'
}

function suiteSummaries(suite: MemoryBenchSuite) {
  if (suite.baseline || suite.v2) return { baseline: suite.baseline, v2: suite.v2 }
  const fixture = suite.fixture as {
    baseline?: Record<string, unknown>
    memoryos_enabled?: Record<string, unknown>
  } | undefined
  return { baseline: fixture?.baseline, v2: fixture?.memoryos_enabled }
}

export function BenchmarkDashboardPage() {
  const benchmark = useQuery({ queryKey: ['memorybench-v2'], queryFn: api.memorybench })
  const coding = useQuery({
    queryKey: ['coding-memory-bench-v2.1'],
    queryFn: api.codingMemoryBench,
  })
  const error = benchmark.error ?? coding.error
  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div>
          <h1>Benchmark Dashboard</h1>
          <p>Frozen V1/V2 regression plus blind V2.1 hard-negative and temporal evidence.</p>
        </div>
        <BarChart3 aria-hidden="true" />
      </header>
      {benchmark.isLoading || coding.isLoading ? (
        <LoadingState label="Loading benchmark evidence" />
      ) : null}
      {error ? (
        <ErrorState
          error={error}
          retry={() => {
            void benchmark.refetch()
            void coding.refetch()
          }}
        />
      ) : null}
      {benchmark.data ? (
        <>
          <section className="benchmark-banner" aria-label="Benchmark release status">
            <div className="benchmark-result">
              <CheckCircle2 aria-hidden="true" />
              <span><strong>Measured gates pass</strong><small>{benchmark.data.release_gates.note}</small></span>
            </div>
            <dl>
              <div><dt>Commit</dt><dd><code>{benchmark.data.git.commit.slice(0, 10)}</code></dd></div>
              <div><dt>Config</dt><dd><code>{benchmark.data.config_hash.slice(0, 10)}</code></dd></div>
              <div><dt>Seed</dt><dd>{benchmark.data.seed}</dd></div>
              <div><dt>Generated</dt><dd>{formatTime(benchmark.data.generated_at)}</dd></div>
            </dl>
          </section>
          <aside className="benchmark-blocker" role="note">
            <AlertTriangle aria-hidden="true" />
            <div><strong>Real-model Agent A/B: external blocker</strong><p>Fixture results validate the paired harness and bootstrap confidence intervals only. No real coding-model effect is claimed.</p></div>
          </aside>
          {coding.data ? (
            <section className="benchmark-table panel" aria-labelledby="coding-benchmark-title">
              <header className="panel-header">
                <h2 id="coding-benchmark-title">
                  <FlaskConical aria-hidden="true" />CodingMemoryBench Fixture Regression
                </h2>
                <span>
                  {coding.data.blind_protocol.gold_loaded_only_by_scorer
                    ? 'Gold isolated'
                    : 'Isolation failed'}
                </span>
              </header>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr><th>Mode</th><th>Recall@5</th><th>Temporal</th><th>Conflict F1</th><th>Model evidence</th></tr>
                  </thead>
                  <tbody>
                    {Object.entries(coding.data.modes).map(([name, mode]) => (
                      <tr key={name}>
                        <td><strong>{name}</strong></td>
                        <td>{mode.retrieval_recall_at_5.toFixed(3)}</td>
                        <td>{mode.temporal_accuracy.toFixed(3)}</td>
                        <td>{mode.conflict.f1.toFixed(3)}</td>
                        <td>{mode.model_status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {coding.data.modes.v2?.perfect_score_warning ? (
                <p className="benchmark-warning">
                  <AlertTriangle aria-hidden="true" />
                  {coding.data.modes.v2.perfect_score_warning}
                </p>
              ) : null}
            </section>
          ) : null}
          <section className="benchmark-table panel" aria-labelledby="benchmark-suites-title">
            <header className="panel-header"><h2 id="benchmark-suites-title"><FlaskConical aria-hidden="true" />MemoryBench suites</h2><span>{Object.keys(benchmark.data.suites).length} suites</span></header>
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Suite</th><th>N</th><th>Evidence</th><th>V1 baseline</th><th>V2</th><th>Gate</th></tr></thead>
                <tbody>{Object.entries(benchmark.data.suites).map(([name, suite]) => {
                  const summaries = suiteSummaries(suite)
                  const isAgent = name === 'agent_ab'
                  const passed = suite.gate?.passed ?? suite.truthfulness_gate?.passed ?? true
                  return (
                    <tr key={name}>
                      <td><strong>{suite.suite}</strong></td>
                      <td>{suite.sample_size.toLocaleString()}</td>
                      <td><span className="evidence-label">{suite.evidence_type ?? (isAgent ? 'fixture' : 'measured')}</span></td>
                      <td>{metric(summaries.baseline)}</td>
                      <td>{metric(summaries.v2)}</td>
                      <td><span className={isAgent ? 'gate-blocked' : passed ? 'gate-pass' : 'gate-fail'}>{isAgent ? 'Fixture only' : passed ? 'Pass' : 'Fail'}</span></td>
                    </tr>
                  )
                })}</tbody>
              </table>
            </div>
          </section>
          <section className="benchmark-footnote"><Cpu aria-hidden="true" /><p>Provider: <code>{String(benchmark.data.provider_policy.default)}</code>. Full prompts are not recorded. Quality and speed remain separate measurements.</p></section>
        </>
      ) : null}
    </div>
  )
}
