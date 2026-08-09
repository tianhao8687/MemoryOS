import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, Check, CircleHelp, X } from 'lucide-react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import { StatusBadge } from '../components/StatusBadge'

export function PossibleConflictsPage() {
  const queryClient = useQueryClient()
  const queue = useQuery({ queryKey: ['possible-conflicts'], queryFn: api.possibleConflicts })
  const resolve = useMutation({
    mutationFn: ({ id, confirmed }: { id: string; confirmed: boolean }) =>
      api.resolvePossibleConflict(id, confirmed, 'Reviewed in the Possible Conflicts queue'),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['possible-conflicts'] }),
  })
  const error = queue.error ?? resolve.error
  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div><h1>Possible Conflicts</h1><p>Uncertain pairs, bounded model evidence, abstentions, and manual resolution.</p></div>
        <CircleHelp aria-hidden="true" />
      </header>
      <aside className="benchmark-blocker possible-conflict-note">
        <AlertCircle aria-hidden="true" />
        <div><strong>Abstention is safe</strong><p>Possible and abstained rows never rewrite accepted truth. Only confirmed review outcomes become conflict evidence.</p></div>
      </aside>
      {error ? <ErrorState error={error} retry={() => void queue.refetch()} /> : null}
      {queue.isLoading ? <LoadingState label="Loading possible conflicts" /> : null}
      {queue.data && !queue.data.length ? (
        <EmptyState title="No uncertain pairs" detail="Deterministic decisions do not enter this queue." />
      ) : null}
      <div className="possible-conflict-list">
        {queue.data?.map((item) => (
          <article className="panel possible-conflict-card" key={item.id}>
            <header>
              <div><span>Pair {item.id.slice(0, 8)}</span><h2>{item.left_claim_id.slice(0, 8)} ↔ {item.right_claim_id.slice(0, 8)}</h2></div>
              <StatusBadge status={item.status} />
            </header>
            <dl>
              <div><dt>Rule result</dt><dd>{item.deterministic_relationship} · {Math.round(item.deterministic_confidence * 100)}%</dd></div>
              <div><dt>Model result</dt><dd>{item.model_result.relationship ?? 'not called'} · {Math.round((item.model_result.confidence ?? 0) * 100)}%</dd></div>
              <div><dt>Prompt</dt><dd><code>{item.prompt_version ?? 'none'}</code></dd></div>
              <div><dt>Provider</dt><dd><code>{item.provider_fingerprint ?? 'offline / unavailable'}</code></dd></div>
            </dl>
            <p>{item.model_result.explanation ?? item.reason}</p>
            <footer>
              <code title={item.evidence_hash}>evidence {item.evidence_hash.slice(0, 16)}…</code>
              {item.status === 'possible' || item.status === 'abstained' ? (
                <span>
                  <button className="button secondary" type="button" onClick={() => resolve.mutate({ id: item.id, confirmed: false })}><X aria-hidden="true" /> Dismiss</button>
                  <button className="button warning" type="button" onClick={() => resolve.mutate({ id: item.id, confirmed: true })}><Check aria-hidden="true" /> Confirm conflict</button>
                </span>
              ) : <small>Resolved {item.resolved_at ? new Date(item.resolved_at).toLocaleString() : 'by model routing'}</small>}
            </footer>
          </article>
        ))}
      </div>
    </div>
  )
}
