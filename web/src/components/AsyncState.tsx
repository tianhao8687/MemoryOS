import { AlertCircle, Database, RefreshCw } from 'lucide-react'

export function LoadingState({ label = 'Loading project memory' }: { label?: string }) {
  return (
    <div className="state-panel" aria-busy="true" aria-label={label}>
      <div className="skeleton-line wide" />
      <div className="skeleton-line" />
      <div className="skeleton-line short" />
    </div>
  )
}

export function ErrorState({ error, retry }: { error: Error; retry?: () => void }) {
  return (
    <div className="state-panel state-error" role="alert">
      <AlertCircle aria-hidden="true" />
      <div>
        <strong>MemoryOS could not load this view</strong>
        <p>{error.message}</p>
      </div>
      {retry ? (
        <button className="button secondary" onClick={retry} type="button">
          <RefreshCw aria-hidden="true" size={15} /> Retry
        </button>
      ) : null}
    </div>
  )
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string
  detail: string
  action?: React.ReactNode
}) {
  return (
    <div className="state-panel state-empty" role="status">
      <Database aria-hidden="true" />
      <strong>{title}</strong>
      <p>{detail}</p>
      {action}
    </div>
  )
}
