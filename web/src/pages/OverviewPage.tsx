import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight, CheckCircle2, Clock3, Database, ShieldCheck } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import { formatTime, shortId } from '../lib/format'

const contextSections = [
  { key: 'CURRENT DECISIONS', title: 'Active decisions', query: 'decision', icon: CheckCircle2 },
  { key: 'ACTIVE CONSTRAINTS', title: 'Constraints', query: 'constraint', icon: ShieldCheck },
  { key: 'KNOWN FAILURES / DO NOT REPEAT', title: 'Known failures', query: 'failure', icon: AlertTriangle },
]

export function OverviewPage() {
  const navigate = useNavigate()
  const status = useQuery({ queryKey: ['status'], queryFn: api.status })
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: api.repositories })
  const conflicts = useQuery({ queryKey: ['conflicts'], queryFn: api.conflicts })
  const timeline = useQuery({ queryKey: ['timeline', 8], queryFn: () => api.timeline(8) })
  const repository = repositories.data?.[0]
  const context = useQuery({
    queryKey: ['context', repository?.stable_key],
    queryFn: () => api.context(repository?.stable_key ?? 'default', repository?.default_branch ?? 'main', 'project architecture constraints failures preferences current task'),
    enabled: repositories.isSuccess,
  })
  const error = status.error ?? repositories.error ?? conflicts.error ?? timeline.error ?? context.error
  return (
    <div className="page overview-page">
      <header className="page-header"><div><h1>Project memory</h1><p>Verified context for the current repository and branch.</p></div></header>
      {error ? <ErrorState error={error} /> : null}
      <section aria-labelledby="current-context-title">
        <div className="section-heading"><h2 id="current-context-title">Current context</h2><span>{context.data?.retrieval_mode ?? 'FTS5'} retrieval</span></div>
        {context.isLoading ? <LoadingState /> : (
          <div className="context-ledger">
            {contextSections.map(({ key, title, query, icon: Icon }) => {
              const memories = context.data?.sections[key] ?? []
              return (
                <article key={key}>
                  <header><Icon aria-hidden="true" /><h3>{title}</h3><button type="button" onClick={() => void navigate(`/memories?q=${query}`)}>View all</button></header>
                  {memories.length ? <ul>{memories.slice(0, 5).map((memory) => (
                    <li key={memory.id}><span><code>{shortId(memory.id)}</code>{memory.title}</span><small>{memory.scope_type}</small></li>
                  ))}</ul> : <p className="ledger-empty">No relevant {title.toLowerCase()}.</p>}
                </article>
              )
            })}
          </div>
        )}
      </section>
      <div className="overview-band">
        <section className="health-panel" aria-labelledby="health-title">
          <header><h2 id="health-title"><Database aria-hidden="true" />Repository health</h2><span>Local database</span></header>
          {status.data ? <dl className="health-metrics">
            <div><dt>Provenance</dt><dd>{Math.round(status.data.provenance_rate * 100)}%</dd><dd className="metric-note">{status.data.sources} sources</dd></div>
            <div><dt>Total memories</dt><dd>{Object.values(status.data.counts).reduce((sum, count) => sum + count, 0)}</dd><dd className="metric-note">{status.data.counts.candidate} candidates</dd></div>
            <div><dt>Active decisions</dt><dd>{status.data.counts.active}</dd><dd className="metric-note">{status.data.counts.expired} expired</dd></div>
            <div><dt>Open conflicts</dt><dd>{status.data.conflicts}</dd><dd className="metric-note">needs review</dd></div>
            <div><dt>Storage</dt><dd>SQLite</dd><dd className="metric-note">WAL + FTS5</dd></div>
          </dl> : <LoadingState />}
        </section>
        <section className="conflict-callout" aria-labelledby="conflict-title">
          <header><h2 id="conflict-title"><AlertTriangle aria-hidden="true" />{conflicts.data?.length ? 'Conflict detected' : 'Memory is consistent'}</h2></header>
          {conflicts.data?.[0] ? <>
            <p><code>{conflicts.data[0].semantic_key}</code> has an active value and a new candidate.</p>
            <button className="button warning" type="button" onClick={() => void navigate('/conflicts')}>Resolve conflict <ArrowRight aria-hidden="true" /></button>
          </> : <p>No unresolved semantic-key conflicts.</p>}
        </section>
      </div>
      <section className="panel recent-panel" aria-labelledby="recent-title">
        <header className="panel-header"><h2 id="recent-title"><Clock3 aria-hidden="true" />Recent memory</h2><button type="button" onClick={() => void navigate('/timeline')}>View timeline</button></header>
        {timeline.data?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>Time</th><th>Action</th><th>ID</th><th>Actor</th></tr></thead><tbody>{timeline.data.map((event) => (
          <tr key={event.id}><td>{formatTime(event.timestamp)}</td><td>{event.action.replaceAll('_', ' ')}</td><td><code>{shortId(event.entity_id)}</code></td><td>{event.actor}</td></tr>
        ))}</tbody></table></div> : <EmptyState title="No recent memory" detail="Add a source-backed candidate to start the project timeline." />}
      </section>
    </div>
  )
}
