import { useMutation, useQuery } from '@tanstack/react-query'
import { Bug, CheckCircle2, Search, XCircle } from 'lucide-react'
import { FormEvent, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState } from '../components/AsyncState'
import { StatusBadge } from '../components/StatusBadge'
import { shortId } from '../lib/format'

export function RetrievalDebuggerPage() {
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: api.repositories })
  const repository = repositories.data?.[0]
  const [task, setTask] = useState('Which current architecture decisions constrain this task?')
  const [budget, setBudget] = useState(6000)
  const debug = useMutation({
    mutationFn: () =>
      api.debugContext(
        repository?.stable_key ?? '',
        repository?.default_branch ?? 'main',
        task,
        budget,
      ),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (repository && task.trim()) debug.mutate()
  }

  const included = debug.data?.manifest?.filter((item) => item.included) ?? []
  const excluded = debug.data?.manifest?.filter((item) => !item.included) ?? []
  const intent = debug.data?.query_plan?.intent

  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div>
          <h1>Retrieval Debugger</h1>
          <p>Trace query intent, candidate channels, RRF features, filtering, and final budget use.</p>
        </div>
        <Bug aria-hidden="true" />
      </header>
      <form className="debug-form panel" onSubmit={submit}>
        <label className="field span-2">
          <span>Coding task</span>
          <textarea value={task} onChange={(event) => setTask(event.target.value)} rows={3} required />
        </label>
        <label className="field">
          <span>Repository</span>
          <input value={repository?.stable_key ?? ''} readOnly aria-label="Repository scope" />
        </label>
        <label className="field budget-field">
          <span>Character budget: <strong>{budget.toLocaleString()}</strong></span>
          <input
            type="range"
            min="500"
            max="20000"
            step="500"
            value={budget}
            onChange={(event) => setBudget(Number(event.target.value))}
          />
        </label>
        <button className="button primary span-2" type="submit" disabled={!repository || debug.isPending}>
          <Search aria-hidden="true" />{debug.isPending ? 'Tracing…' : 'Run trace'}
        </button>
      </form>
      {debug.error ? <ErrorState error={debug.error} retry={() => debug.mutate()} /> : null}
      {!debug.data && !debug.error && !debug.isPending ? (
        <EmptyState title="Ready to trace" detail="Run a task to inspect every retrieval and context decision." />
      ) : null}
      {debug.data ? (
        <div className="debug-layout" aria-live="polite">
          <section className="debug-summary panel" aria-labelledby="debug-plan-title">
            <header className="panel-header"><h2 id="debug-plan-title">Deterministic Query Planner</h2><StatusBadge status={debug.data.truth_state ?? 'unknown'} /></header>
            <dl>
              <div><dt>Intent</dt><dd>{(typeof intent === 'string' ? intent : 'broad_search').replaceAll('_', ' ')}</dd></div>
              <div><dt>Pipeline</dt><dd><code>{debug.data.retrieval_mode}</code></dd></div>
              <div><dt>Reranker</dt><dd>{debug.data.debug?.reranker ?? 'disabled'}</dd></div>
              <div><dt>Budget</dt><dd>{debug.data.characters_used.toLocaleString()} / {debug.data.budget.toLocaleString()}</dd></div>
              <div><dt>Run ID</dt><dd><code>{debug.data.retrieval_run_id ? shortId(debug.data.retrieval_run_id) : '—'}</code></dd></div>
              <div><dt>Config</dt><dd><code>{debug.data.debug?.config_hash.slice(0, 10) ?? '—'}</code></dd></div>
            </dl>
          </section>
          <section className="panel debug-candidates" aria-labelledby="candidate-trace-title">
            <header className="panel-header"><h2 id="candidate-trace-title">Candidate ledger</h2><span>{included.length} in · {excluded.length} out</span></header>
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Decision</th><th>Memory</th><th>Utility / cost</th><th>Truth</th><th>Freshness</th><th>Reason</th></tr></thead>
                <tbody>{debug.data.manifest?.map((item) => (
                  <tr key={item.memory_id}>
                    <td>{item.included ? <span className="trace-in"><CheckCircle2 aria-hidden="true" />Included</span> : <span className="trace-out"><XCircle aria-hidden="true" />Excluded</span>}</td>
                    <td><code>{shortId(item.memory_id)}</code></td>
                    <td>{item.utility.toFixed(4)} / {item.cost}</td>
                    <td>{item.truth_state}</td>
                    <td>{item.freshness}</td>
                    <td>{item.inclusion_reason ?? item.exclusion_reason ?? '—'}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </section>
          <section className="panel compiled-context" aria-labelledby="compiled-context-title">
            <header className="panel-header"><h2 id="compiled-context-title">Compiled context</h2><span>{debug.data.characters_used.toLocaleString()} chars</span></header>
            <pre>{debug.data.text}</pre>
          </section>
        </div>
      ) : null}
    </div>
  )
}
