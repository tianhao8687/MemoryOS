import { AlertTriangle, CheckCircle2, Circle, Clock3, XCircle } from 'lucide-react'
import type { FreshnessState, MemoryStatus, TruthState } from '../types'

interface Props {
  status:
    | MemoryStatus
    | TruthState
    | FreshnessState
    | 'PASS'
    | 'WARN'
    | 'FAIL'
    | 'needs_review'
    | 'contested'
    | 'possible'
    | 'confirmed'
    | 'dismissed'
    | 'abstained'
}

export function StatusBadge({ status }: Props) {
  const label = status.replaceAll('_', ' ')
  const Icon =
    status === 'active' || status === 'PASS' || status === 'resolved' || status === 'fresh' || status === 'confirmed'
      ? CheckCircle2
      : status === 'candidate' || status === 'WARN' || status === 'moved' || status === 'suspect' || status === 'possible' || status === 'abstained'
        ? Clock3
        : status === 'FAIL' || status === 'rejected' || status === 'forgotten' || status === 'stale' || status === 'dismissed'
          ? XCircle
          : status === 'needs_review' || status === 'contested'
            ? AlertTriangle
            : Circle
  return (
    <span className={`status-badge status-${status}`}>
      <Icon aria-hidden="true" size={13} />
      {label}
    </span>
  )
}
