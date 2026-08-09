import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Copy, GitCompareArrows, History, ShieldAlert, XCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import { StatusBadge } from '../components/StatusBadge'
import { formatTime, shortId } from '../lib/format'
import type { ConflictRecord, MemoryRecord } from '../types'

function CompareMemory({ memory, candidate }: { memory: MemoryRecord; candidate?: boolean }) {
  return <article className={`compare-memory ${candidate ? 'is-candidate' : 'is-current'}`}><header><span>{candidate ? 'Candidate' : 'Current (active decision)'}</span><StatusBadge status={memory.status} /></header><h3>{memory.title}</h3><dl className="definition-grid"><div><dt>ID</dt><dd><code>{shortId(memory.id)}</code></dd></div><div><dt>Scope</dt><dd>{memory.scope_type}</dd></div><div><dt>Created</dt><dd>{formatTime(memory.created_at)}</dd></div><div><dt>Confidence</dt><dd>{Math.round(memory.confidence * 100)}%</dd></div><div><dt>Importance</dt><dd>{Math.round(memory.importance * 100)}%</dd></div><div><dt>Key</dt><dd><code>{memory.key}</code></dd></div></dl><div className="decision-content"><button className="icon-button" type="button" aria-label="Copy decision content" onClick={() => void navigator.clipboard.writeText(memory.content)}><Copy aria-hidden="true" /></button><p>{memory.content}</p></div></article>
}

export function ConflictsPage() {
  const client = useQueryClient()
  const conflicts = useQuery({ queryKey: ['conflicts'], queryFn: api.conflicts })
  const [selected, setSelected] = useState<ConflictRecord | null>(null)
  const [strategy, setStrategy] = useState('supersede')
  const [rationale, setRationale] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const available = conflicts.data ?? []
    if (!selected || !available.some((item) => item.candidate.id === selected.candidate.id)) {
      setSelected(available[0] ?? null)
    }
  }, [conflicts.data, selected])
  const code = useMemo(() => selected ? shortId(selected.candidate.id).toUpperCase() : '', [selected])
  const resolve = useMutation({ mutationFn: () => api.resolveConflict(selected!.candidate.id, strategy, rationale), onSuccess: async () => { setConfirmation(''); setRationale(''); await client.invalidateQueries(); setSelected(null) }, onError: (reason: Error) => setError(reason.message) })
  return (
    <div className="page conflicts-page">
      <header className="page-header"><div><h1>Conflicts</h1><p>Compare sources before changing an active project decision.</p></div></header>
      {conflicts.isLoading ? <LoadingState /> : null}
      {conflicts.error ? <ErrorState error={conflicts.error} retry={() => void conflicts.refetch()} /> : null}
      {conflicts.data && !conflicts.data.length ? <EmptyState title="No unresolved conflicts" detail="New values never overwrite active decisions silently." /> : null}
      {conflicts.data?.length ? <div className="conflict-layout">
        <aside className="conflict-queue" aria-label="Conflict queue"><header><AlertTriangle aria-hidden="true" />Queue <strong>{conflicts.data.length}</strong></header>{conflicts.data.map((conflict) => <button key={conflict.candidate.id} className={selected?.candidate.id === conflict.candidate.id ? 'is-selected' : ''} type="button" onClick={() => { setSelected(conflict); setConfirmation(''); setError(null) }}><span>{conflict.candidate.title}</span><code>{conflict.semantic_key}</code><small>{formatTime(conflict.candidate.created_at)}</small></button>)}</aside>
        {selected ? <section className="conflict-workspace" aria-labelledby="conflict-detail-title"><header className="conflict-meta"><div><h2 id="conflict-detail-title"><GitCompareArrows aria-hidden="true" />{selected.candidate.title}</h2><StatusBadge status="needs_review" /></div><dl><div><dt>Scope</dt><dd>{selected.candidate.scope_type}/{selected.candidate.scope_key}</dd></div><div><dt>Key</dt><dd><code>{selected.semantic_key}</code></dd></div><div><dt>Detected</dt><dd>{formatTime(selected.candidate.created_at)}</dd></div></dl></header><div className="comparison-grid"><CompareMemory memory={selected.current[0]} /><span className="versus">vs</span><CompareMemory memory={selected.candidate} candidate /></div><div className="resolution-panel"><h3>Resolution</h3><div className="resolution-options"><label className={strategy === 'supersede' ? 'is-selected' : ''}><input type="radio" name="strategy" value="supersede" checked={strategy === 'supersede'} onChange={(event) => setStrategy(event.target.value)} /><CheckCircle2 aria-hidden="true" /><span><strong>Supersede current</strong><small>Replace the active decision and preserve history.</small></span></label><label className={strategy === 'keep_both' ? 'is-selected' : ''}><input type="radio" name="strategy" value="keep_both" checked={strategy === 'keep_both'} onChange={(event) => setStrategy(event.target.value)} /><History aria-hidden="true" /><span><strong>Keep both</strong><small>Mark both decisions as explicit alternatives.</small></span></label><label className={strategy === 'reject' ? 'is-selected danger-choice' : 'danger-choice'}><input type="radio" name="strategy" value="reject" checked={strategy === 'reject'} onChange={(event) => setStrategy(event.target.value)} /><XCircle aria-hidden="true" /><span><strong>Reject candidate</strong><small>Keep current and reject this proposal.</small></span></label></div><div className="resolution-fields"><label className="field"><span>Rationale (required)</span><textarea value={rationale} onChange={(event) => setRationale(event.target.value)} rows={4} placeholder="Explain why this resolution is correct…" /></label><div className="safe-confirm"><ShieldAlert aria-hidden="true" /><div><strong>Safe confirmation</strong><p>Type <code>{code}</code> to confirm this resolution.</p><input aria-label="Confirmation code" value={confirmation} onChange={(event) => setConfirmation(event.target.value.toUpperCase())} placeholder={code} /><button className="button primary" disabled={confirmation !== code || !rationale.trim() || resolve.isPending} onClick={() => resolve.mutate()} type="button">{resolve.isPending ? 'Resolving…' : 'Confirm resolution'}</button></div></div></div>{error ? <p className="form-error" role="alert">{error}</p> : null}</div></section> : null}
      </div> : null}
    </div>
  )
}
