import { useQuery } from '@tanstack/react-query'
import { FileCheck2 } from 'lucide-react'
import { api } from '../api/client'
import { ErrorState, LoadingState } from '../components/AsyncState'
import { TimelineList } from '../components/TimelineList'

export function AuditPage() {
  const audit = useQuery({ queryKey: ['audit', 200], queryFn: () => api.audit(200) })
  return <div className="page"><header className="page-header"><div><h1>Audit</h1><p>Transport-independent evidence for every lifecycle mutation.</p></div><FileCheck2 aria-hidden="true" /></header><section className="panel timeline-panel" aria-label="Audit event log">{audit.isLoading ? <LoadingState /> : null}{audit.error ? <ErrorState error={audit.error} retry={() => void audit.refetch()} /> : null}{audit.data ? <TimelineList events={audit.data} detailed /> : null}</section></div>
}
