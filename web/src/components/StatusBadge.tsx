import { AlertTriangle, CheckCircle2, Circle, Clock3, XCircle } from 'lucide-react'
import type { MemoryStatus } from '../types'

interface Props {
  status: MemoryStatus | 'PASS' | 'WARN' | 'FAIL' | 'needs_review'
}

export function StatusBadge({ status }: Props) {
  const label = status.replaceAll('_', ' ')
  const Icon =
    status === 'active' || status === 'PASS'
      ? CheckCircle2
      : status === 'candidate' || status === 'WARN'
        ? Clock3
        : status === 'FAIL' || status === 'rejected' || status === 'forgotten'
          ? XCircle
          : status === 'needs_review'
            ? AlertTriangle
            : Circle
  return (
    <span className={`status-badge status-${status}`}>
      <Icon aria-hidden="true" size={13} />
      {label}
    </span>
  )
}
