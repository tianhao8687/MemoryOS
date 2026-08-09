import { useQuery } from '@tanstack/react-query'
import { Clock3 } from 'lucide-react'
import { api } from '../api/client'
import { ErrorState, LoadingState } from '../components/AsyncState'
import { TimelineList } from '../components/TimelineList'

export function TimelinePage() {
  const timeline = useQuery({ queryKey: ['timeline', 200], queryFn: () => api.timeline(200) })
  return <div className="page"><header className="page-header"><div><h1>Timeline</h1><p>Every proposal, confirmation, replacement, expiration, and forget event in UTC.</p></div><Clock3 aria-hidden="true" /></header><section className="panel timeline-panel" aria-label="Memory timeline">{timeline.isLoading ? <LoadingState /> : null}{timeline.error ? <ErrorState error={timeline.error} retry={() => void timeline.refetch()} /> : null}{timeline.data ? <TimelineList events={timeline.data} /> : null}</section></div>
}
