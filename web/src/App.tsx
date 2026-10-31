import { Fragment, useEffect } from "react"
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom"

import { DebugMenu, EmptyState, ErrorBoundary, Sidebar } from "@/components/shared"
import { BrokenPage } from "@/components/shared/DebugMenu"
import { routeExternalLinks } from "@/lib/externalLinks"
import { useFeatureSettings } from "@/lib/features"
import { isDesktop } from "@/lib/desktop"
import { CodexAccountProvider } from "@/lib/agent/CodexAccountProvider"
import { HOME, pageAt } from "@/lib/nav"
import { TeamPage } from "./pages/team/TeamPage"
import { AgentPage } from "@/pages/agent/AgentPage"
import { InstallPage } from "@/pages/install/InstallPage"
import { LogbookReader } from "@/pages/people/logbook/LogbookReader"
import { MemoryPage } from "@/pages/memory/MemoryPage"
import { OnboardingPage } from "@/pages/onboarding/OnboardingPage"
import { PeoplePage } from "@/pages/people/PeoplePage"
import { ReviewPage } from "@/pages/review/ReviewPage"
import { SearchesPage } from "@/pages/searches/SearchesPage"
import { SetsPage } from "@/pages/sets/SetsPage"
import { AccountsPage } from "@/pages/settings/AccountsPage"
import { SettingsPage } from "@/pages/settings/SettingsPage"
import { TasksPage } from "@/pages/tasks/TasksPage"

// The side nav, then the page: the page owns its own scrolling and any panel of its own.
function Shell() {
  const { pathname } = useLocation()
  const page = pageAt(pathname)
  useEffect(() => {
    document.title = `${page.label} · Powerpacks`
  }, [page])
  // Links to other sites open in the system browser (a no-op outside the desktop app).
  useEffect(routeExternalLinks, [])
  return (
    <div className="page-enter grid h-dvh grid-cols-[auto_minmax(0,1fr)] overflow-hidden">
      <Sidebar />
      <ErrorBoundary at={pathname}>
        <Outlet />
      </ErrorBoundary>
    </div>
  )
}

// The paths the server answers with this app (packs/shared/web/app.py PAGE_PATHS). /searches
// is a layout route, so picking a run (/searches/run?run_id=…) keeps the page mounted. Setup
// (/install) sits in the shell too, so the other pages are a click away while it runs. The
// review flow at / stands outside the shell: it is the user's entry point, with its own top
// bar. Any other client-side path lands on HOME.
export function App() {
  const features = useFeatureSettings()
  // The desktop app has no browser bar to leave the review by: there it keeps the side nav.
  const desktop = isDesktop()
  const AccountProvider = desktop ? CodexAccountProvider : Fragment
  return (
    <BrowserRouter>
      <AccountProvider>
        <Routes>
          {desktop ? null : <Route path="/" element={<ReviewPage />} />}
          {/* The onboarding is the whole window, as a first run is: no side nav. */}
          <Route
            path="/onboarding/:step?"
            element={
              <ErrorBoundary at="/onboarding">
                <OnboardingPage />
              </ErrorBoundary>
            }
          />
          <Route element={<Shell />}>
            {desktop ? (
              <Route
                path="/"
                element={
                  <div className="min-h-0 overflow-y-auto">
                    <ReviewPage />
                  </div>
                }
              />
            ) : null}
            <Route path="/install" element={<InstallPage />} />
            <Route path="/agent" element={<AgentPage />} />
            <Route path="/people" element={<PeoplePage />}>
              <Route index element={null} />
              <Route path="logbook" element={<LogbookReader />} />
            </Route>
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/searches" element={<SearchesPage />}>
              <Route index element={null} />
              <Route path="run" element={null} />
            </Route>
            <Route
              path="/sets"
              element={
                features.isPending ? (
                  <EmptyState>Loading…</EmptyState>
                ) : features.data?.sets ? (
                  <SetsPage />
                ) : (
                  <Navigate to={HOME.href} replace />
                )
              }
            />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/team" element={<TeamPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            {desktop ? <Route path="/debug/broken" element={<BrokenPage />} /> : null}
            <Route path="*" element={<Navigate to={HOME.href} replace />} />
          </Route>
        </Routes>
        <DebugMenu />
      </AccountProvider>
    </BrowserRouter>
  )
}
