import { Activity, UserRound } from 'lucide-react'
import { formatTime, shortId } from '../lib/format'
import type { AuditEvent } from '../types'
import { EmptyState } from './AsyncState'

export function TimelineList({ events, detailed = false }: { events: AuditEvent[]; detailed?: boolean }) {
  if (!events.length) return <EmptyState title="No audit events yet" detail="Every write and lifecycle change will appear here." />
  return (
    <ol className={`timeline-list ${detailed ? 'is-detailed' : ''}`}>
      {events.map((event) => (
        <li key={event.id}>
          <span className="timeline-marker"><Activity aria-hidden="true" /></span>
          <time dateTime={event.timestamp}>{formatTime(event.timestamp)}</time>
          <div>
            <strong>{event.action.replaceAll('_', ' ')}</strong>
            <p><code>{shortId(event.entity_id)}</code> · {event.entity_type}</p>
            {detailed ? <pre>{JSON.stringify(event.details, null, 2)}</pre> : null}
          </div>
          <span className="timeline-actor"><UserRound aria-hidden="true" />{event.actor}</span>
        </li>
      ))}
    </ol>
  )
}
