import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderGit2, GitBranch, Link2, MapPin, ScanSearch } from 'lucide-react'
import { FormEvent, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'

export function ProjectsPage() {
  const [path, setPath] = useState('.')
  const client = useQueryClient()
  const repositories = useQuery({ queryKey: ['repositories'], queryFn: api.repositories })
  const detect = useMutation({
    mutationFn: api.detectRepository,
    onSuccess: async () => client.invalidateQueries({ queryKey: ['repositories'] }),
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    detect.mutate(path)
  }
  return (
    <div className="page">
      <header className="page-header"><div><h1>Projects</h1><p>Stable Git identities keep repository memory intact when paths move.</p></div></header>
      <section className="panel project-detect" aria-labelledby="detect-title">
        <div><ScanSearch aria-hidden="true" /><div><h2 id="detect-title">Detect a Git workspace</h2><p>MemoryOS reads Git metadata only; it never bulk-ingests source files.</p></div></div>
        <form onSubmit={submit}><label className="field"><span>Repository path</span><input value={path} onChange={(event) => setPath(event.target.value)} /></label><button className="button primary" disabled={detect.isPending} type="submit">{detect.isPending ? 'Detecting…' : 'Detect repository'}</button></form>
        {detect.error ? <p className="form-error" role="alert">{detect.error.message}</p> : null}
      </section>
      {repositories.isLoading ? <LoadingState /> : null}
      {repositories.error ? <ErrorState error={repositories.error} retry={() => void repositories.refetch()} /> : null}
      {repositories.data?.length ? <section className="project-list" aria-label="Known repositories">{repositories.data.map((repository) => (
        <article key={repository.id} className="project-row">
          <FolderGit2 aria-hidden="true" />
          <div><h2>{repository.name}</h2><code>{repository.stable_key}</code></div>
          <dl><div><dt><MapPin aria-hidden="true" />Path</dt><dd>{repository.path}</dd></div><div><dt><Link2 aria-hidden="true" />Remote</dt><dd>{repository.remote_url ?? 'Local repository'}</dd></div><div><dt><GitBranch aria-hidden="true" />Branch</dt><dd>{repository.default_branch ?? 'Unknown'}</dd></div></dl>
        </article>
      ))}</section> : repositories.isSuccess ? <EmptyState title="No projects detected" detail="Point MemoryOS at a Git repository to establish a stable project scope." /> : null}
    </div>
  )
}
