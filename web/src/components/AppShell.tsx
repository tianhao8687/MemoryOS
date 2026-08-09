import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Archive,
  Boxes,
  Clock3,
  Database,
  FileClock,
  FolderGit2,
  GitBranch,
  LayoutDashboard,
  Menu,
  Plus,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import { FormEvent, useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { api } from '../api/client'

const navigation = [
  { to: '/', label: 'Overview', icon: LayoutDashboard },
  { to: '/projects', label: 'Projects', icon: FolderGit2 },
  { to: '/memories', label: 'Memories', icon: Archive },
  { to: '/candidates', label: 'Candidates', icon: Sparkles },
  { to: '/timeline', label: 'Timeline', icon: Clock3 },
  { to: '/conflicts', label: 'Conflicts', icon: AlertTriangle },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/audit', label: 'Audit', icon: FileClock },
]

function AddMemoryDialog({
  open,
  onClose,
  repository,
}: {
  open: boolean
  onClose: () => void
  repository: string
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal()
    if (!open && dialog.current?.open) dialog.current.close()
  }, [open])
  const mutation = useMutation({
    mutationFn: api.propose,
    onSuccess: async () => {
      await queryClient.invalidateQueries()
      onClose()
    },
    onError: (reason: Error) => setError(reason.message),
  })
  function value(data: FormData, key: string): string {
    const entry = data.get(key)
    return typeof entry === 'string' ? entry : ''
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const content = value(data, 'content')
    const ttl = value(data, 'ttl_seconds')
    mutation.mutate({
      scope_type: 'repository',
      scope_key: value(data, 'scope_key'),
      memory_type: value(data, 'memory_type'),
      category: value(data, 'category'),
      key: value(data, 'key') || null,
      title: value(data, 'title'),
      content,
      confidence: 0.9,
      importance: Number(value(data, 'importance')),
      ttl_seconds: ttl ? Number(ttl) : null,
      created_by: 'manual',
      activate_immediately: data.has('activate_immediately'),
      sensitivity: 'normal',
      source: { source_type: 'manual', source_ref: 'ui:manual', excerpt: content },
    })
  }
  return (
    <dialog className="modal" ref={dialog} onClose={onClose} aria-labelledby="add-memory-title">
      <form onSubmit={submit}>
        <header className="modal-header">
          <div>
            <h2 id="add-memory-title">Add memory</h2>
            <p>Save for review, or explicitly activate a manual entry now.</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close dialog">
            <X aria-hidden="true" />
          </button>
        </header>
        <div className="form-grid">
          <label className="field span-2">
            <span>Title</span>
            <input autoFocus name="title" required maxLength={300} />
          </label>
          <label className="field span-2">
            <span>Content</span>
            <textarea name="content" required rows={5} maxLength={20000} />
          </label>
          <label className="field">
            <span>Repository scope</span>
            <input name="scope_key" required defaultValue={repository || 'default'} />
          </label>
          <label className="field">
            <span>Semantic key</span>
            <input name="key" placeholder="architecture.backend" />
          </label>
          <label className="field">
            <span>Memory type</span>
            <select name="memory_type" defaultValue="project">
              <option value="project">Project</option>
              <option value="procedural">Procedural</option>
              <option value="episodic">Episodic</option>
              <option value="preference">Preference</option>
              <option value="semantic">Semantic</option>
              <option value="working">Working</option>
            </select>
          </label>
          <label className="field">
            <span>Category</span>
            <select name="category" defaultValue="decision">
              <option value="decision">Decision</option>
              <option value="constraint">Constraint</option>
              <option value="failure">Failure</option>
              <option value="preference">Preference</option>
              <option value="state">State</option>
              <option value="note">Note</option>
            </select>
          </label>
          <label className="field">
            <span>TTL seconds (optional)</span>
            <input name="ttl_seconds" type="number" min="1" max="315360000" placeholder="604800" />
          </label>
          <label className="field span-2">
            <span>Importance</span>
            <input name="importance" type="range" min="0" max="1" step="0.1" defaultValue="0.7" />
          </label>
          <label className="checkbox-field span-2">
            <input name="activate_immediately" type="checkbox" />
            <span><strong>Activate immediately</strong><small>Manual entries only. The activation is written to the audit timeline.</small></span>
          </label>
        </div>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <footer className="modal-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className="button primary" disabled={mutation.isPending} type="submit">
            {mutation.isPending ? 'Saving…' : 'Save memory'}
          </button>
        </footer>
      </form>
    </dialog>
  )
}

export function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [search, setSearch] = useState('')
  const navigate = useNavigate()
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: api.repositories })
  const status = useQuery({ queryKey: ['status'], queryFn: api.status, refetchInterval: 30000 })
  const repository = repositories.data?.[0]
  function submitSearch(event: FormEvent) {
    event.preventDefault()
    void navigate(`/memories?q=${encodeURIComponent(search)}`)
  }
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <aside className={`sidebar ${mobileOpen ? 'is-open' : ''}`} aria-label="Primary navigation">
        <div className="brand"><Database aria-hidden="true" /><strong>MemoryOS</strong></div>
        <nav>
          {navigation.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === '/'} onClick={() => setMobileOpen(false)}>
              <Icon aria-hidden="true" /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-status">
          <span><Boxes aria-hidden="true" />Local store</span>
          <strong>{status.data ? 'OK' : '—'}</strong>
          <small>v{status.data?.version ?? '1.0.0'}</small>
        </div>
      </aside>
      {mobileOpen ? <button className="scrim" onClick={() => setMobileOpen(false)} aria-label="Close navigation" /> : null}
      <div className="workspace">
        <header className="topbar">
          <button className="mobile-menu icon-button" onClick={() => setMobileOpen(true)} aria-label="Open navigation">
            <Menu aria-hidden="true" />
          </button>
          <div className="repository-context">
            <span><Database aria-hidden="true" />{repository?.name ?? 'No repository'}</span>
            <span><GitBranch aria-hidden="true" />{repository?.default_branch ?? 'main'}</span>
            <span className="local-only"><ShieldCheck aria-hidden="true" />Local only</span>
          </div>
          <form className="top-search" role="search" onSubmit={submitSearch}>
            <Search aria-hidden="true" />
            <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} aria-label="Search memories" placeholder="Search memories…" />
          </form>
          <div className="top-actions">
            <button className="button secondary" type="button" onClick={() => setAddOpen(true)}><Plus aria-hidden="true" />Add memory</button>
            <button className="button secondary review-button" type="button" onClick={() => void navigate('/candidates')}><Sparkles aria-hidden="true" />Review candidates</button>
          </div>
        </header>
        <main id="main-content" tabIndex={-1}><Outlet /></main>
      </div>
      <AddMemoryDialog open={addOpen} onClose={() => setAddOpen(false)} repository={repository?.stable_key ?? ''} />
    </div>
  )
}
