import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  Database,
  Download,
  HardDrive,
  Radio,
  RefreshCcw,
  ShieldCheck,
  Waypoints,
  WifiOff,
} from 'lucide-react'
import { api } from '../api/client'
import { ErrorState, LoadingState } from '../components/AsyncState'
import { StatusBadge } from '../components/StatusBadge'

export function SettingsPage() {
  const queryClient = useQueryClient()
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const doctor = useQuery({ queryKey: ['doctor'], queryFn: api.doctor })
  const vectors = useQuery({ queryKey: ['vector-index'], queryFn: api.vectorIndex })
  const backup = useMutation({ mutationFn: api.backup })
  const exportData = useMutation({ mutationFn: api.exportData })
  const rebuild = useMutation({
    mutationFn: api.rebuildVectorIndex,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['vector-index'] })
      await queryClient.invalidateQueries({ queryKey: ['doctor'] })
    },
  })
  const error = settings.error ?? doctor.error ?? vectors.error
  const actionError = backup.error ?? exportData.error ?? rebuild.error

  return (
    <div className="page settings-page">
      <header className="page-header">
        <div><h1>Settings</h1><p>Local runtime, provider, index, safety, and backup state.</p></div>
      </header>
      {error ? <ErrorState error={error} /> : null}
      {settings.isLoading || doctor.isLoading || vectors.isLoading ? <LoadingState /> : null}
      {settings.data ? (
        <div className="settings-layout">
          <section className="panel settings-section">
            <header><h2><HardDrive aria-hidden="true" />Local storage</h2></header>
            <dl className="settings-list">
              <div><dt><Database aria-hidden="true" />Database path</dt><dd><code>{settings.data.database_path}</code></dd></div>
              <div><dt><Archive aria-hidden="true" />Backup path</dt><dd><code>{settings.data.backup_path}</code></dd></div>
              <div><dt><Radio aria-hidden="true" />MCP server</dt><dd><StatusBadge status="PASS" /> {settings.data.mcp_status}</dd></div>
              <div><dt><WifiOff aria-hidden="true" />Provider</dt><dd>{settings.data.provider_status} · deterministic fallback available</dd></div>
              <div><dt><ShieldCheck aria-hidden="true" />Telemetry</dt><dd>{settings.data.telemetry ? 'Enabled' : 'Disabled'}</dd></div>
            </dl>
          </section>

          <section className="panel settings-section">
            <header><h2><ShieldCheck aria-hidden="true" />Doctor</h2><StatusBadge status={doctor.data?.overall ?? 'WARN'} /></header>
            <ul className="doctor-list">
              {doctor.data?.checks.map((check) => (
                <li key={check.name}><StatusBadge status={check.status} /><span><strong>{check.name.replaceAll('_', ' ')}</strong><small>{check.detail}</small></span></li>
              ))}
            </ul>
          </section>

          <section className="panel settings-section vector-section">
            <header>
              <h2><Waypoints aria-hidden="true" />Vector index</h2>
              <button className="button secondary" type="button" disabled={rebuild.isPending} onClick={() => rebuild.mutate()}>
                <RefreshCcw aria-hidden="true" /> {rebuild.isPending ? 'Rebuilding…' : 'Rebuild'}
              </button>
            </header>
            {!vectors.data?.length ? <p className="settings-empty">No model namespace has been built. FTS5 remains active.</p> : null}
            <ul className="vector-list">
              {vectors.data?.map((vector) => (
                <li key={vector.namespace}>
                  <div><StatusBadge status={vector.status === 'ready' ? 'PASS' : 'WARN'} /><strong>{vector.provider} / {vector.model}</strong></div>
                  <code>{vector.namespace}</code>
                  <span>{vector.item_count.toLocaleString()} vectors · {vector.dimensions} dimensions · {vector.backend}</span>
                  {vector.unavailable_reason ? <small>{vector.unavailable_reason}</small> : null}
                </li>
              ))}
            </ul>
          </section>

          <section className="panel settings-section backup-section">
            <header><h2><Archive aria-hidden="true" />Backup &amp; export</h2></header>
            <p>Create a versioned SQLite backup or a schema-validated portable JSONL export.</p>
            <div>
              <button className="button primary" type="button" disabled={backup.isPending} onClick={() => backup.mutate()}><Archive aria-hidden="true" />{backup.isPending ? 'Creating…' : 'Create backup'}</button>
              <button className="button secondary" type="button" disabled={exportData.isPending} onClick={() => exportData.mutate()}><Download aria-hidden="true" />{exportData.isPending ? 'Exporting…' : 'Export JSONL'}</button>
            </div>
            {backup.data ? <p className="success-message" role="status">Backup saved to <code>{backup.data.path}</code></p> : null}
            {exportData.data ? <p className="success-message" role="status">Export saved to <code>{exportData.data.path}</code></p> : null}
            {actionError ? <p className="form-error" role="alert">{actionError.message}</p> : null}
          </section>
        </div>
      ) : null}
    </div>
  )
}
