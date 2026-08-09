import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Boxes, FlaskConical, Save } from 'lucide-react'
import { useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import { formatTime, shortId } from '../lib/format'
import type { ConsolidationProposal } from '../types'

function display(value: unknown, fallback: string): string {
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return value === null || value === undefined ? fallback : JSON.stringify(value)
}

function ProposalCard({ item, persisted }: { item: ConsolidationProposal; persisted?: boolean }) {
  return (
    <article className={`consolidation-card ${item.status === 'contested' ? 'is-contested' : ''}`}>
      <header>
        <div><span>{persisted ? 'Inbox' : 'Dry-run preview'}</span><h2>{display(item.proposal.predicate, 'Pattern')}</h2></div>
        <strong>{item.status}</strong>
      </header>
      <p>{display(item.proposal.object, 'No proposed object')}</p>
      <dl>
        <div><dt>Sources</dt><dd>{item.source_memory_ids.length}</dd></div>
        <div><dt>Counterevidence</dt><dd>{item.counterevidence.length}</dd></div>
        <div><dt>Confidence</dt><dd>{Math.round(Number(item.proposal.confidence ?? 0) * 100)}%</dd></div>
      </dl>
      <footer>{item.source_memory_ids.slice(0, 4).map((id) => <code key={id}>{shortId(id)}</code>)}</footer>
    </article>
  )
}

export function ConsolidationPage() {
  const client = useQueryClient()
  const [preview, setPreview] = useState<ConsolidationProposal[]>([])
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: api.repositories })
  const inbox = useQuery({ queryKey: ['consolidations'], queryFn: api.consolidations })
  const scopeKey = repositories.data?.[0]?.stable_key ?? ''
  const run = useMutation({
    mutationFn: (dryRun: boolean) => api.consolidate(scopeKey, dryRun),
    onSuccess: async (result, dryRun) => {
      setPreview(result.proposals)
      if (!dryRun) await client.invalidateQueries({ queryKey: ['consolidations'] })
    },
  })

  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div>
          <h1>Consolidation Inbox</h1>
          <p>Promote repeated episodic evidence without hiding counterevidence or lineage.</p>
        </div>
        <Boxes aria-hidden="true" />
      </header>
      <section className="consolidation-actions panel" aria-labelledby="consolidation-run-title">
        <div>
          <h2 id="consolidation-run-title">Repository pattern scan</h2>
          <p>Requires at least three independent sources spanning seven days.</p>
        </div>
        <button
          className="button secondary"
          type="button"
          disabled={!scopeKey || run.isPending}
          onClick={() => run.mutate(true)}
        ><FlaskConical aria-hidden="true" />Preview</button>
        <button
          className="button primary"
          type="button"
          disabled={!scopeKey || run.isPending}
          onClick={() => run.mutate(false)}
        ><Save aria-hidden="true" />Save proposals</button>
      </section>
      {run.error ? <p className="form-error" role="alert">{run.error.message}</p> : null}
      {preview.length ? (
        <section className="consolidation-section" aria-labelledby="preview-title">
          <div className="section-heading"><h2 id="preview-title">Latest run</h2><span>{preview.length} proposal(s)</span></div>
          <div className="consolidation-grid">{preview.map((item, index) => <ProposalCard item={item} key={item.id ?? index} />)}</div>
        </section>
      ) : null}
      {inbox.isLoading ? <LoadingState label="Loading consolidation inbox" /> : null}
      {inbox.error ? <ErrorState error={inbox.error} retry={() => void inbox.refetch()} /> : null}
      {inbox.data && !inbox.data.length && !preview.length ? (
        <EmptyState
          title="No consolidation candidates"
          detail="Repeated episodic claims stay separate until the source and time-span thresholds are met."
          action={<button className="button secondary" type="button" disabled={!scopeKey} onClick={() => run.mutate(true)}>Run a dry scan</button>}
        />
      ) : null}
      {inbox.data?.length ? (
        <section className="consolidation-section" aria-labelledby="inbox-title">
          <div className="section-heading"><h2 id="inbox-title">Saved inbox</h2><span>{inbox.data.length} candidate(s)</span></div>
          <div className="consolidation-grid">{inbox.data.map((row) => (
            <article className={`consolidation-card ${row.status === 'contested' ? 'is-contested' : ''}`} key={row.id}>
              <header><div><span>{row.scope_type}/{row.scope_key}</span><h2>{row.predicate}</h2></div><strong>{row.status}</strong></header>
              <p>{display(row.proposal.object, 'No proposed object')}</p>
              <dl><div><dt>Sources</dt><dd>{row.source_memory_ids.length}</dd></div><div><dt>Counterevidence</dt><dd>{row.counterevidence.length}</dd></div><div><dt>Created</dt><dd>{formatTime(row.created_at)}</dd></div></dl>
            </article>
          ))}</div>
        </section>
      ) : null}
    </div>
  )
}
