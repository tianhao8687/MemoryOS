import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Clock3, FileKey2, Save, X, XCircle } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { formatTime } from '../lib/format'
import type { MemoryRecord } from '../types'
import { ErrorState, LoadingState } from './AsyncState'

export function CandidateInspector({ memory, onClose }: { memory: MemoryRecord; onClose: () => void }) {
  const client = useQueryClient()
  const [title, setTitle] = useState(memory.title)
  const [content, setContent] = useState(memory.content)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { setTitle(memory.title); setContent(memory.content); setError(null) }, [memory])
  const explain = useQuery({ queryKey: ['explain', memory.id], queryFn: () => api.explain(memory.id) })
  async function refreshAndClose() {
    await client.invalidateQueries()
    onClose()
  }
  const update = useMutation({
    mutationFn: () => api.update(memory.id, { title, content }),
    onSuccess: async () => client.invalidateQueries({ queryKey: ['candidates'] }),
    onError: (reason: Error) => setError(reason.message),
  })
  const confirm = useMutation({
    mutationFn: async (editFirst: boolean) => {
      if (editFirst && (title !== memory.title || content !== memory.content)) await api.update(memory.id, { title, content })
      return api.confirm(memory.id)
    },
    onSuccess: refreshAndClose,
    onError: (reason: Error) => setError(reason instanceof ApiError && reason.code === 'CONFLICT_DETECTED' ? 'This candidate conflicts with active memory. Open Conflicts to choose a resolution.' : reason.message),
  })
  const reject = useMutation({ mutationFn: () => api.reject(memory.id), onSuccess: refreshAndClose, onError: (reason: Error) => setError(reason.message) })
  function save(event: FormEvent) { event.preventDefault(); update.mutate() }
  return (
    <aside className="inspector candidate-inspector" aria-label={`Review candidate: ${memory.title}`}>
      <header className="inspector-header"><div><span className="candidate-label"><Clock3 aria-hidden="true" />Candidate (not confirmed)</span><h2>{memory.title}</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label="Close candidate details"><X aria-hidden="true" /></button></header>
      <div className="inspector-content">
        <form onSubmit={save} className="candidate-form">
          <label className="field"><span>Title</span><input value={title} onChange={(event) => setTitle(event.target.value)} required /></label>
          <label className="field"><span>Content</span><textarea value={content} onChange={(event) => setContent(event.target.value)} rows={6} required /></label>
          <div className="form-grid">
            <label className="field"><span>Type</span><input value={memory.memory_type} readOnly /></label>
            <label className="field"><span>Category</span><input value={memory.category} readOnly /></label>
            <label className="field"><span>Scope</span><input value={memory.scope_type} readOnly /></label>
            <label className="field"><span>Importance</span><input value={`${Math.round(memory.importance * 100)}%`} readOnly /></label>
            <label className="field"><span>Confidence</span><input value={`${Math.round(memory.confidence * 100)}%`} readOnly /></label>
            <label className="field"><span>TTL / validity</span><input value={memory.ttl_seconds ? `${memory.ttl_seconds}s` : 'Indefinite'} readOnly /></label>
          </div>
          <button className="text-button" type="submit" disabled={update.isPending}><Save aria-hidden="true" />Save edits</button>
        </form>
        {memory.memory_type === 'working' && !memory.ttl_seconds ? <p className="form-warning" role="status">Working memory has no TTL and will remain active until it is forgotten or given a validity limit.</p> : null}
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        {explain.isLoading ? <LoadingState label="Loading candidate provenance" /> : null}
        {explain.error ? <ErrorState error={explain.error} /> : null}
        {explain.data ? <section><h3><FileKey2 aria-hidden="true" />Why this memory?</h3>{explain.data.sources.map((source) => <div className="source-block" key={source.id}><dl><div><dt>source_type</dt><dd>{source.source_type}</dd></div><div><dt>source_ref</dt><dd><code>{source.source_ref}</code></dd></div><div><dt>captured_at</dt><dd>{formatTime(source.captured_at)}</dd></div><div><dt>content_hash</dt><dd><code>{source.content_hash.slice(0, 24)}…</code></dd></div><div><dt>created_by</dt><dd>{memory.created_by}</dd></div><div><dt>status</dt><dd>{memory.status}</dd></div></dl></div>)}</section> : null}
      </div>
      <footer className="inspector-actions three-actions">
        <button className="button primary" type="button" onClick={() => confirm.mutate(false)} disabled={confirm.isPending}><CheckCircle2 aria-hidden="true" />Confirm</button>
        <button className="button secondary" type="button" onClick={() => confirm.mutate(true)} disabled={confirm.isPending}><Save aria-hidden="true" />Edit &amp; confirm</button>
        <button className="button danger" type="button" onClick={() => reject.mutate()} disabled={reject.isPending}><XCircle aria-hidden="true" />Reject</button>
      </footer>
    </aside>
  )
}
