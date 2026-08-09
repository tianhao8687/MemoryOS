import { useQuery } from '@tanstack/react-query'
import { CalendarClock, Search, ShieldCheck } from 'lucide-react'
import { FormEvent, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import { StatusBadge } from '../components/StatusBadge'
import type { ClaimRecord } from '../types'

function objectLabel(claim: ClaimRecord): string {
  if (typeof claim.object_value === 'string') return claim.object_value
  return JSON.stringify(claim.object_value)
}

export function CurrentTruthPage() {
  const [draft, setDraft] = useState('project')
  const [query, setQuery] = useState('project')
  const [validTime, setValidTime] = useState('')
  const [knownTime, setKnownTime] = useState('')
  const truth = useQuery({
    queryKey: ['current-truth', query, validTime, knownTime],
    queryFn: () =>
      api.currentTruth({
        query,
        as_of_valid_time: validTime || null,
        as_known_at: knownTime || null,
      }),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (draft.trim()) setQuery(draft.trim())
  }

  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div>
          <h1>Current Truth</h1>
          <p>Inspect resolved, contested, stale, and historical claims at a point in time.</p>
        </div>
        {truth.data ? <StatusBadge status={truth.data.state} /> : <ShieldCheck aria-hidden="true" />}
      </header>

      <form className="truth-query panel" onSubmit={submit}>
        <label className="field truth-search">
          <span>Subject, predicate, or natural-language query</span>
          <span className="input-with-icon">
            <Search aria-hidden="true" />
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="production database"
              required
            />
          </span>
        </label>
        <label className="field">
          <span>Valid at</span>
          <input
            type="datetime-local"
            value={validTime}
            onChange={(event) => setValidTime(event.target.value)}
          />
        </label>
        <label className="field">
          <span>Known at</span>
          <input
            type="datetime-local"
            value={knownTime}
            onChange={(event) => setKnownTime(event.target.value)}
          />
        </label>
        <button className="button primary" type="submit">
          <CalendarClock aria-hidden="true" /> Resolve truth
        </button>
      </form>

      {truth.isLoading ? <LoadingState label="Resolving current truth" /> : null}
      {truth.error ? <ErrorState error={truth.error} retry={() => void truth.refetch()} /> : null}
      {truth.data && !truth.data.truths.length ? (
        <EmptyState
          title="No matching claims"
          detail="Try an entity alias, predicate, or broader project term. MemoryOS will not invent a truth without evidence."
        />
      ) : null}
      {truth.data?.truths.length ? (
        <div className="truth-list" aria-live="polite">
          {truth.data.truths.map((group) => {
            const claims =
              group.state === 'contested' ? group.conflicting_claims : group.accepted_claims
            return (
              <article className={`truth-card truth-${group.state}`} key={`${group.subject.id}:${group.predicate}`}>
                <header>
                  <div>
                    <span>{group.subject.entity_type}</span>
                    <h2>{group.subject.canonical_name}</h2>
                    <code>{group.predicate}</code>
                  </div>
                  <StatusBadge status={group.state} />
                </header>
                <ul className="claim-stack" aria-label={`${group.predicate} claims`}>
                  {claims.map((claim) => (
                    <li key={claim.id}>
                      <strong>{objectLabel(claim)}</strong>
                      <span>{claim.modality} · {Math.round(claim.confidence * 100)}% confidence</span>
                      <small>
                        {claim.status} · freshness {claim.stale_state}
                        {claim.version_number ? ` · version ${claim.version_number}` : ''}
                      </small>
                      {claim.transaction_from ? (
                        <details className="claim-version-detail">
                          <summary>Transaction and validity</summary>
                          <dl>
                            <div><dt>Known from</dt><dd>{new Date(claim.transaction_from).toLocaleString()}</dd></div>
                            <div><dt>Known until</dt><dd>{claim.transaction_to ? new Date(claim.transaction_to).toLocaleString() : 'current'}</dd></div>
                            <div><dt>Valid from</dt><dd>{claim.valid_from ? new Date(claim.valid_from).toLocaleString() : 'unbounded'}</dd></div>
                            <div><dt>Valid until</dt><dd>{claim.valid_to ? new Date(claim.valid_to).toLocaleString() : 'unbounded'}</dd></div>
                            <div><dt>Reason</dt><dd>{claim.reason ?? 'not recorded'}</dd></div>
                            <div><dt>Actor</dt><dd><code>{claim.actor ?? 'unknown'}</code></dd></div>
                          </dl>
                        </details>
                      ) : null}
                    </li>
                  ))}
                </ul>
                <footer>
                  <span>{group.evidence.length} evidence record(s)</span>
                  <span>{group.resolution_history.length} resolution event(s)</span>
                </footer>
              </article>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
