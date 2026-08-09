import { formatTime, shortId } from '../lib/format'
import type { MemoryRecord } from '../types'
import { EmptyState } from './AsyncState'
import { StatusBadge } from './StatusBadge'

export function MemoryTable({
  memories,
  selectedId,
  onSelect,
  emptyTitle = 'No memories found',
  emptyDetail = 'Adjust filters or add a source-backed project memory.',
  selectable = false,
}: {
  memories: MemoryRecord[]
  selectedId?: string
  onSelect: (memory: MemoryRecord) => void
  emptyTitle?: string
  emptyDetail?: string
  selectable?: boolean
}) {
  if (!memories.length) return <EmptyState title={emptyTitle} detail={emptyDetail} />
  return (
    <div className="table-scroll">
      <table className="data-table memory-table">
        <thead>
          <tr>
            {selectable ? <th className="checkbox-cell"><span className="sr-only">Select</span></th> : null}
            <th>Title</th>
            <th>Type / category</th>
            <th>Scope</th>
            <th>Source</th>
            <th>Confidence</th>
            <th>Updated</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {memories.map((memory) => (
            <tr
              key={memory.id}
              className={selectedId === memory.id ? 'is-selected' : undefined}
              onClick={() => onSelect(memory)}
            >
              {selectable ? (
                <td className="checkbox-cell">
                  <input
                    type="checkbox"
                    aria-label={`Select ${memory.title}`}
                    checked={selectedId === memory.id}
                    onChange={() => onSelect(memory)}
                    onClick={(event) => event.stopPropagation()}
                  />
                </td>
              ) : null}
              <td>
                <button className="row-title" type="button" onClick={() => onSelect(memory)}>
                  {memory.title}
                </button>
                <code>{memory.key ?? shortId(memory.id)}</code>
              </td>
              <td><span>{memory.memory_type}</span><small>{memory.category}</small></td>
              <td><span>{memory.scope_type}</span><small>{memory.scope_key}</small></td>
              <td><span>{memory.created_by}</span><small>{shortId(memory.id)}</small></td>
              <td>
                <span>{Math.round(memory.confidence * 100)}%</span>
                <span className="meter" aria-hidden="true"><i style={{ width: `${memory.confidence * 100}%` }} /></span>
              </td>
              <td>{formatTime(memory.updated_at)}</td>
              <td><StatusBadge status={memory.status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
