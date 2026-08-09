import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileCode2, Filter, GitCommitHorizontal, RefreshCw } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import { StatusBadge } from '../components/StatusBadge'
import { formatTime, shortId } from '../lib/format'
import type { FreshnessState } from '../types'

const filters: Array<'all' | FreshnessState> = [
  'all',
  'fresh',
  'moved',
  'suspect',
  'stale',
  'unknown',
]

export function GitFreshnessPage() {
  const client = useQueryClient()
  const [filter, setFilter] = useState<(typeof filters)[number]>('all')
  const [replacement, setReplacement] = useState(true)
  const [refreshingId, setRefreshingId] = useState<string | null>(null)
  const freshness = useQuery({ queryKey: ['freshness'], queryFn: api.freshness })
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: api.repositories })
  const repositoryPath = repositories.data?.[0]?.path ?? ''
  const refresh = useMutation({
    mutationFn: (memoryId: string) => api.refresh(memoryId, repositoryPath, replacement),
    onMutate: (memoryId) => setRefreshingId(memoryId),
    onSettled: async () => {
      setRefreshingId(null)
      await client.invalidateQueries({ queryKey: ['freshness'] })
      await client.invalidateQueries({ queryKey: ['candidates'] })
    },
  })
  const rows = useMemo(
    () =>
      (freshness.data ?? []).filter((item) => filter === 'all' || item.freshness === filter),
    [filter, freshness.data],
  )

  return (
    <div className="page intelligence-page">
      <header className="page-header">
        <div>
          <h1>Git Freshness</h1>
          <p>Track anchored evidence across unchanged blobs, moves, material edits, and deletion.</p>
        </div>
        <GitCommitHorizontal aria-hidden="true" />
      </header>
      <div className="toolbar">
        <label className="select-control">
          <Filter aria-hidden="true" />
          <span className="sr-only">Freshness filter</span>
          <select value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}>
            {filters.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <label className="checkbox-field compact-check">
          <input
            type="checkbox"
            checked={replacement}
            onChange={(event) => setReplacement(event.target.checked)}
          />
          <span><strong>Create replacement candidate when evidence changed</strong></span>
        </label>
      </div>
      {freshness.isLoading ? <LoadingState label="Checking Git freshness" /> : null}
      {freshness.error ? <ErrorState error={freshness.error} retry={() => void freshness.refetch()} /> : null}
      {freshness.data && !freshness.data.length ? (
        <EmptyState
          title="No Git anchors"
          detail="Attach a memory claim to a file or symbol to begin freshness tracking."
        />
      ) : null}
      {freshness.data?.length ? (
        <section className="panel table-panel" aria-labelledby="freshness-table-title">
          <header className="panel-header">
            <h2 id="freshness-table-title"><FileCode2 aria-hidden="true" />Anchored evidence</h2>
            <span>{rows.length} shown</span>
          </header>
          <div className="table-scroll">
            <table className="data-table freshness-table">
              <thead><tr><th>Memory</th><th>Path / symbol</th><th>State</th><th>Commit</th><th>Checked</th><th>Action</th></tr></thead>
              <tbody>{rows.map((item) => (
                <tr key={item.anchor_id}>
                  <td><strong>{item.memory_title}</strong><code>{shortId(item.memory_id)}</code></td>
                  <td><code>{item.path}</code><small>{item.symbol_fqn ?? 'file excerpt'}</small></td>
                  <td><StatusBadge status={item.freshness} /></td>
                  <td><code>{item.commit_sha.slice(0, 8)}</code></td>
                  <td>{item.checked_at ? formatTime(item.checked_at) : 'Not checked'}</td>
                  <td>
                    <button
                      className="button secondary"
                      type="button"
                      disabled={!repositoryPath || refresh.isPending}
                      onClick={() => refresh.mutate(item.memory_id)}
                    >
                      <RefreshCw aria-hidden="true" />
                      {refreshingId === item.memory_id ? 'Refreshing…' : 'Refresh'}
                    </button>
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </section>
      ) : null}
      {refresh.error ? <p className="form-error" role="alert">{refresh.error.message}</p> : null}
    </div>
  )
}
