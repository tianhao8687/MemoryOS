import { useQuery } from '@tanstack/react-query'
import { Search, Sparkles } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'
import { api } from '../api/client'
import { ErrorState, LoadingState } from '../components/AsyncState'
import { CandidateInspector } from '../components/CandidateInspector'
import { MemoryTable } from '../components/MemoryTable'
import type { MemoryRecord } from '../types'

export function CandidatesPage() {
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [selected, setSelected] = useState<MemoryRecord | null>(null)
  const candidates = useQuery({ queryKey: ['candidates', submitted], queryFn: () => api.search({ q: submitted, status: 'candidate', include_history: true, limit: 200 }) })
  useEffect(() => {
    if (!selected && candidates.data?.items[0]) setSelected(candidates.data.items[0].memory)
  }, [candidates.data, selected])
  function search(event: FormEvent) { event.preventDefault(); setSubmitted(query) }
  const rows = candidates.data?.items.map((item) => item.memory) ?? []
  return (
    <div className={`page candidates-page ${selected ? 'with-inspector' : ''}`}>
      <header className="page-header"><div><h1>Candidates</h1><p>Agent and extractor proposals stay quarantined until explicit review.</p></div></header>
      <div className="queue-tabs" aria-label="Candidate state"><span className="queue-tab is-active"><Sparkles aria-hidden="true" />Queue <strong>{candidates.data?.total ?? 0}</strong></span></div>
      <section className="toolbar" aria-label="Candidate filters"><form className="filter-search" role="search" onSubmit={search}><Search aria-hidden="true" /><input type="search" aria-label="Search candidates" placeholder="Search candidates…" value={query} onChange={(event) => setQuery(event.target.value)} /><button className="button primary" type="submit">Search</button></form></section>
      <section className="panel table-panel" aria-label="Candidate queue">
        <header className="panel-header"><span>{selected ? '1 selected' : 'Select a candidate to review'}</span><span>Candidate writes cannot bypass confirmation</span></header>
        {candidates.isLoading ? <LoadingState /> : null}
        {candidates.error ? <ErrorState error={candidates.error} retry={() => void candidates.refetch()} /> : null}
        {candidates.data ? <MemoryTable memories={rows} selectedId={selected?.id} onSelect={setSelected} selectable emptyTitle="Candidate queue is clear" emptyDetail="Agent proposals will appear here before they can affect context." /> : null}
      </section>
      {selected ? <CandidateInspector memory={selected} onClose={() => setSelected(null)} /> : null}
    </div>
  )
}
