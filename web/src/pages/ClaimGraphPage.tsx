import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Network, Search } from 'lucide-react'
import { FormEvent, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import { StatusBadge } from '../components/StatusBadge'
import { shortId } from '../lib/format'
import type { ClaimRecord } from '../types'

function claimObject(claim: ClaimRecord): string {
  return typeof claim.object_value === 'string'
    ? claim.object_value
    : JSON.stringify(claim.object_value)
}

export function ClaimGraphPage() {
  const [draft, setDraft] = useState('project')
  const [query, setQuery] = useState('project')
  const graph = useQuery({
    queryKey: ['claim-graph', query],
    queryFn: () => api.claimGraph({ query }),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (draft.trim()) setQuery(draft.trim())
  }

  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div>
          <h1>Claim Graph</h1>
          <p>A bounded neighborhood for one topic, with relation method and confidence exposed.</p>
        </div>
        <Network aria-hidden="true" />
      </header>
      <form className="graph-query" onSubmit={submit} role="search">
        <label className="filter-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Graph topic</span>
          <input
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Search a subject or decision"
          />
          <button className="button primary" type="submit">Load neighborhood</button>
        </label>
      </form>
      {graph.isLoading ? <LoadingState label="Loading claim graph" /> : null}
      {graph.error ? <ErrorState error={graph.error} retry={() => void graph.refetch()} /> : null}
      {graph.data && !graph.data.nodes.length ? (
        <EmptyState title="No local graph" detail="No normalized claims match this topic yet." />
      ) : null}
      {graph.data?.nodes.length ? (
        <div className="claim-graph-layout">
          <section className="panel graph-nodes" aria-labelledby="graph-nodes-title">
            <header className="panel-header">
              <h2 id="graph-nodes-title"><Network aria-hidden="true" />Claims</h2>
              <StatusBadge status={graph.data.state} />
            </header>
            <ol>
              {graph.data.nodes.map((node) => (
                <li key={node.id}>
                  <span className="graph-node-index">{shortId(node.id)}</span>
                  <div>
                    <strong>{node.subject?.canonical_name ?? 'Unknown subject'}</strong>
                    <p><code>{node.predicate}</code> <ArrowRight aria-hidden="true" /> {claimObject(node)}</p>
                    <small>{node.status} · {node.modality} · {Math.round(node.confidence * 100)}%</small>
                  </div>
                </li>
              ))}
            </ol>
          </section>
          <section className="panel graph-relations" aria-labelledby="graph-relations-title">
            <header className="panel-header">
              <h2 id="graph-relations-title">Relations</h2>
              <span>{graph.data.edges.length}</span>
            </header>
            {graph.data.edges.length ? (
              <ol>
                {graph.data.edges.map((edge) => (
                  <li key={edge.id}>
                    <div><code>{shortId(edge.from)}</code><ArrowRight aria-hidden="true" /><code>{shortId(edge.to)}</code></div>
                    <strong>{edge.type.replaceAll('_', ' ')}</strong>
                    <span>{edge.method} · {Math.round(edge.confidence * 100)}%</span>
                    <p>{edge.explanation}</p>
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState title="No relation edges" detail="The matching claims are currently independent." />
            )}
          </section>
        </div>
      ) : null}
    </div>
  )
}
