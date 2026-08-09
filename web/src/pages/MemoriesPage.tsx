import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Filter, Search, Trash2 } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { ErrorState, LoadingState } from '../components/AsyncState'
import { MemoryInspector } from '../components/MemoryInspector'
import { MemoryTable } from '../components/MemoryTable'
import type { MemoryRecord, MemoryStatus } from '../types'

export function MemoriesPage() {
  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState(params.get('q') ?? '')
  const [selected, setSelected] = useState<MemoryRecord | null>(null)
  const status = (params.get('status') ?? '') as MemoryStatus | ''
  const client = useQueryClient()
  const memories = useQuery({
    queryKey: ['memories', params.toString()],
    queryFn: () => api.search({ q: params.get('q'), status, include_history: Boolean(status), limit: 200 }),
  })
  const forget = useMutation({
    mutationFn: api.forget,
    onSuccess: async () => {
      setSelected(null)
      await client.invalidateQueries({ queryKey: ['memories'] })
      await client.invalidateQueries({ queryKey: ['status'] })
    },
  })
  useEffect(() => {
    const current = params.get('q') ?? ''
    setQuery(current)
  }, [params])
  function search(event: FormEvent) {
    event.preventDefault()
    const next = new URLSearchParams(params)
    if (query) next.set('q', query)
    else next.delete('q')
    setParams(next)
  }
  function changeStatus(value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set('status', value)
    else next.delete('status')
    setParams(next)
  }
  return (
    <div className={`page ${selected ? 'with-inspector' : ''}`}>
      <header className="page-header"><div><h1>Memories</h1><p>Search active context or inspect the complete auditable history.</p></div></header>
      <section className="toolbar" aria-label="Memory filters">
        <form className="filter-search" role="search" onSubmit={search}><Search aria-hidden="true" /><input type="search" aria-label="Search all memory" placeholder="Search title, content, subject, or key" value={query} onChange={(event) => setQuery(event.target.value)} /><button className="button primary" type="submit">Search</button></form>
        <label className="select-control"><Filter aria-hidden="true" /><span className="sr-only">Filter by status</span><select value={status} onChange={(event) => changeStatus(event.target.value)}><option value="">Active only</option><option value="candidate">Candidates</option><option value="active">Active</option><option value="superseded">Superseded</option><option value="expired">Expired</option><option value="forgotten">Forgotten</option><option value="rejected">Rejected</option></select></label>
      </section>
      <section className="panel table-panel" aria-label="Memory search results">
        <header className="panel-header"><div><strong>{memories.data?.total ?? 0}</strong><span> matching memories</span></div><span>{memories.data?.mode ?? 'FTS5'}</span></header>
        {memories.isLoading ? <LoadingState /> : null}
        {memories.error ? <ErrorState error={memories.error} retry={() => void memories.refetch()} /> : null}
        {memories.data ? <MemoryTable memories={memories.data.items.map((item) => item.memory)} selectedId={selected?.id} onSelect={setSelected} /> : null}
      </section>
      {selected ? <MemoryInspector memory={selected} onClose={() => setSelected(null)} actions={
        selected.status === 'active' || selected.status === 'candidate' ? <button className="button danger" type="button" onClick={() => forget.mutate(selected.id)} disabled={forget.isPending}><Trash2 aria-hidden="true" />{forget.isPending ? 'Forgetting…' : 'Forget memory'}</button> : undefined
      } /> : null}
    </div>
  )
}
