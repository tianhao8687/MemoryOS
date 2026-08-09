import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  ArchiveRestore,
  Flame,
  Layers3,
  RefreshCcw,
  Snowflake,
  ThermometerSun,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import type { MemoryHealthRecord, MemoryTemperature } from '../types'

const temperatureIcon = {
  hot: Flame,
  warm: ThermometerSun,
  cold: Snowflake,
  archived: Archive,
}

function HealthRow({
  item,
  selected,
  onSelect,
  onArchive,
  onRestore,
}: {
  item: MemoryHealthRecord
  selected: boolean
  onSelect: (checked: boolean) => void
  onArchive: () => void
  onRestore: () => void
}) {
  const Icon = temperatureIcon[item.temperature]
  const canDistill = item.temperature === 'cold' || item.temperature === 'archived'
  return (
    <tr>
      <td>
        <input
          type="checkbox"
          checked={selected}
          disabled={!canDistill}
          onChange={(event) => onSelect(event.target.checked)}
          aria-label={`Select ${item.title} for distillation`}
        />
      </td>
      <td>
        <strong>{item.title}</strong>
        <code>{item.memory_id}</code>
      </td>
      <td>
        <span className={`temperature-label is-${item.temperature}`}>
          <Icon aria-hidden="true" /> {item.temperature}
        </span>
      </td>
      <td>
        <meter min="0" max="1" value={item.health_score}>
          {Math.round(item.health_score * 100)}%
        </meter>
        <small>{Math.round(item.health_score * 100)}%</small>
      </td>
      <td>{item.retrieval_count}</td>
      <td className="health-explanation">{item.explanation}</td>
      <td>
        {item.temperature === 'archived' ? (
          <button className="button secondary" type="button" onClick={onRestore}>
            <ArchiveRestore aria-hidden="true" /> Restore
          </button>
        ) : (
          <button
            className="button secondary"
            type="button"
            onClick={onArchive}
            title="The server refuses to archive the sole accepted truth support"
          >
            <Archive aria-hidden="true" /> Archive
          </button>
        )}
      </td>
    </tr>
  )
}

export function MemoryHealthPage() {
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<MemoryTemperature | 'all'>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const health = useQuery({ queryKey: ['memory-health'], queryFn: () => api.memoryHealth() })
  const refresh = useMutation({
    mutationFn: api.evaluateMemoryHealth,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memory-health'] }),
  })
  const archive = useMutation({
    mutationFn: api.archiveMemory,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memory-health'] }),
  })
  const restore = useMutation({
    mutationFn: api.restoreMemory,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memory-health'] }),
  })
  const distill = useMutation({
    mutationFn: (ids: string[]) => api.distillMemories(ids),
    onSuccess: async () => {
      setSelected(new Set())
      await queryClient.invalidateQueries()
    },
  })
  const rows = useMemo(
    () => health.data?.filter((item) => filter === 'all' || item.temperature === filter) ?? [],
    [filter, health.data],
  )
  const counts = useMemo(
    () =>
      (health.data ?? []).reduce<Record<string, number>>((result, item) => {
        result[item.temperature] = (result[item.temperature] ?? 0) + 1
        return result
      }, {}),
    [health.data],
  )
  const error = health.error ?? refresh.error ?? archive.error ?? restore.error ?? distill.error

  function toggle(id: string, checked: boolean) {
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div>
          <h1>Memory Health</h1>
          <p>Explainable Hot/Warm/Cold lifecycle, reversible archives, and grounded distillation.</p>
        </div>
        <button
          className="button primary"
          type="button"
          disabled={refresh.isPending}
          onClick={() => refresh.mutate()}
        >
          <RefreshCcw aria-hidden="true" /> {refresh.isPending ? 'Evaluating…' : 'Evaluate health'}
        </button>
      </header>

      <section className="health-summary" aria-label="Memory temperature summary">
        {(['hot', 'warm', 'cold', 'archived'] as MemoryTemperature[]).map((temperature) => {
          const Icon = temperatureIcon[temperature]
          return (
            <button
              className={filter === temperature ? 'is-selected' : ''}
              type="button"
              key={temperature}
              onClick={() => setFilter(filter === temperature ? 'all' : temperature)}
              aria-pressed={filter === temperature}
            >
              <Icon aria-hidden="true" />
              <span>{temperature}</span>
              <strong>{counts[temperature] ?? 0}</strong>
            </button>
          )
        })}
      </section>

      <section className="panel health-actions" aria-label="Distillation actions">
        <div>
          <Layers3 aria-hidden="true" />
          <span><strong>{selected.size} selected</strong><small>Only Cold or Archived memories can be distilled.</small></span>
        </div>
        <button
          className="button secondary"
          type="button"
          disabled={selected.size < 2 || distill.isPending}
          onClick={() => distill.mutate([...selected])}
        >
          <Layers3 aria-hidden="true" /> {distill.isPending ? 'Creating…' : 'Create candidate'}
        </button>
      </section>

      {error ? <ErrorState error={error} retry={() => void health.refetch()} /> : null}
      {health.isLoading ? <LoadingState label="Loading memory health" /> : null}
      {health.data && !health.data.length ? (
        <EmptyState
          title="Health has not been evaluated"
          detail="Run an evaluation to assign explainable temperatures. No memory is archived automatically."
        />
      ) : null}
      {rows.length ? (
        <div className="panel table-scroll">
          <table className="data-table health-table">
            <thead><tr><th><span className="sr-only">Select</span></th><th>Memory</th><th>Temperature</th><th>Score</th><th>Retrievals</th><th>Explanation</th><th>Action</th></tr></thead>
            <tbody>
              {rows.map((item) => (
                <HealthRow
                  key={item.memory_id}
                  item={item}
                  selected={selected.has(item.memory_id)}
                  onSelect={(checked) => toggle(item.memory_id, checked)}
                  onArchive={() => archive.mutate(item.memory_id)}
                  onRestore={() => restore.mutate(item.memory_id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {distill.data ? <p className="success-message" role="status">Candidate created: {distill.data.candidate.title}</p> : null}
    </div>
  )
}
