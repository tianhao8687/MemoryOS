import { Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { AuditPage } from './pages/AuditPage'
import { CandidatesPage } from './pages/CandidatesPage'
import { ConflictsPage } from './pages/ConflictsPage'
import { MemoriesPage } from './pages/MemoriesPage'
import { OverviewPage } from './pages/OverviewPage'
import { ProjectsPage } from './pages/ProjectsPage'
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
        <Route path="timeline" element={<TimelinePage />} />
        <Route path="conflicts" element={<ConflictsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="audit" element={<AuditPage />} />
      </Route>
    </Routes>
  )
}
