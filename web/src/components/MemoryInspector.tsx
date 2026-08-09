import { useQuery } from '@tanstack/react-query'
import { FileKey2, History, Link2, X } from 'lucide-react'
import { api } from '../api/client'
import { formatTime, shortId } from '../lib/format'
import type { MemoryRecord } from '../types'
import { ErrorState, LoadingState } from './AsyncState'
import { StatusBadge } from './StatusBadge'

export function MemoryInspector({
  memory,
  onClose,
  actions,
}: {
  memory: MemoryRecord
  onClose: () => void
  actions?: React.ReactNode
}) {
  const explain = useQuery({
    queryKey: ['explain', memory.id],
    queryFn: () => api.explain(memory.id),
  })
  return (
    <aside className="inspector" aria-label={`Memory details: ${memory.title}`}>
      <header className="inspector-header">
        <div><StatusBadge status={memory.status} /><h2>{memory.title}</h2></div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close details"><X aria-hidden="true" /></button>
      </header>
      <div className="inspector-content">
        <section>
          <h3>Memory content</h3>
          <p className="memory-content">{memory.content}</p>
          <dl className="definition-grid">
            <div><dt>ID</dt><dd><code>{memory.id}</code></dd></div>
            <div><dt>Semantic key</dt><dd><code>{memory.key ?? '—'}</code></dd></div>
            <div><dt>Type</dt><dd>{memory.memory_type} / {memory.category}</dd></div>
            <div><dt>Scope</dt><dd>{memory.scope_type} / {memory.scope_key}</dd></div>
            <div><dt>Confidence</dt><dd>{Math.round(memory.confidence * 100)}%</dd></div>
            <div><dt>Importance</dt><dd>{Math.round(memory.importance * 100)}%</dd></div>
            <div><dt>Created</dt><dd>{formatTime(memory.created_at)}</dd></div>
            <div><dt>TTL</dt><dd>{memory.ttl_seconds ? `${memory.ttl_seconds}s` : 'Indefinite'}</dd></div>
          </dl>
          {memory.memory_type === 'working' && !memory.ttl_seconds ? <p className="form-warning" role="status">Working memory has no TTL and may outlive the task that created it.</p> : null}
        </section>
        {explain.isLoading ? <LoadingState label="Loading provenance" /> : null}
        {explain.error ? <ErrorState error={explain.error} retry={() => void explain.refetch()} /> : null}
        {explain.data ? (
          <>
            <section>
              <h3><FileKey2 aria-hidden="true" />Why this memory?</h3>
              <p>{explain.data.reason}</p>
              {explain.data.sources.map((source) => (
                <div className="source-block" key={source.id}>
                  <div><span>{source.source_type}</span><code>{source.source_ref}</code></div>
                  <p>{source.excerpt}</p>
                  <dl>
                    <div><dt>Captured</dt><dd>{formatTime(source.captured_at)}</dd></div>
                    <div><dt>Hash</dt><dd><code title={source.content_hash}>{source.content_hash.slice(0, 20)}…</code></dd></div>
                  </dl>
                </div>
              ))}
            </section>
            <section>
              <h3><Link2 aria-hidden="true" />Relations</h3>
              {explain.data.relations.length ? explain.data.relations.map((relation) => (
                <p key={relation.id}><strong>{relation.relation_type}</strong> <code>{shortId(relation.to_memory_id)}</code></p>
              )) : <p className="muted">No replacement or alternative links.</p>}
            </section>
            <section>
              <h3><History aria-hidden="true" />History</h3>
              <ol className="mini-timeline">
                {explain.data.audit.map((event) => (
                  <li key={event.id}><span>{event.action}</span><small>{formatTime(event.timestamp)} · {event.actor}</small></li>
                ))}
              </ol>
            </section>
          </>
        ) : null}
      </div>
      {actions ? <footer className="inspector-actions">{actions}</footer> : null}
    </aside>
  )
}
