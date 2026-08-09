import { Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { AuditPage } from './pages/AuditPage'
import { BenchmarkDashboardPage } from './pages/BenchmarkDashboardPage'
import { CandidatesPage } from './pages/CandidatesPage'
import { ClaimGraphPage } from './pages/ClaimGraphPage'
import { ConsolidationPage } from './pages/ConsolidationPage'
import { ConflictsPage } from './pages/ConflictsPage'
import { CurrentTruthPage } from './pages/CurrentTruthPage'
import { GitFreshnessPage } from './pages/GitFreshnessPage'
import { MemoriesPage } from './pages/MemoriesPage'
import { OverviewPage } from './pages/OverviewPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { RetrievalDebuggerPage } from './pages/RetrievalDebuggerPage'
import { SettingsPage } from './pages/SettingsPage'
import { TimelinePage } from './pages/TimelinePage'

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<OverviewPage />} />
        <Route path="projects" element={<ProjectsPage />} />
        <Route path="memories" element={<MemoriesPage />} />
        <Route path="candidates" element={<CandidatesPage />} />
        <Route path="current-truth" element={<CurrentTruthPage />} />
        <Route path="claim-graph" element={<ClaimGraphPage />} />
        <Route path="freshness" element={<GitFreshnessPage />} />
        <Route path="consolidation" element={<ConsolidationPage />} />
        <Route path="retrieval-debugger" element={<RetrievalDebuggerPage />} />
        <Route path="benchmarks" element={<BenchmarkDashboardPage />} />
        <Route path="timeline" element={<TimelinePage />} />
        <Route path="conflicts" element={<ConflictsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="audit" element={<AuditPage />} />
      </Route>
    </Routes>
  )
}
