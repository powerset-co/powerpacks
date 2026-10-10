import { useEffect } from "react"
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom"

import { DebugMenu, Sidebar, SignInModal } from "@/components/shared"
import { isDesktop } from "@/lib/desktop"
import { HOME, pageAt } from "@/lib/nav"
import { AccountsPage } from "@/pages/accounts/AccountsPage"
import { AgentPage } from "@/pages/agent/AgentPage"
import { InstallPage } from "@/pages/install/InstallPage"
import { LogbookReader } from "@/pages/people/logbook/LogbookReader"
import { PeoplePage } from "@/pages/people/PeoplePage"
import { ReviewPage } from "@/pages/review/ReviewPage"
import { SearchesPage } from "@/pages/searches/SearchesPage"
import { SetsPage } from "@/pages/sets/SetsPage"
import { TasksPage } from "@/pages/tasks/TasksPage"

// The side nav, then the page: the page owns its own scrolling and any panel of its own.
function Shell() {
  const page = pageAt(useLocation().pathname)
  useEffect(() => {
    document.title = `${page.label} · Powerpacks`
  }, [page])
  return (
    <div className="page-enter grid h-dvh grid-cols-[auto_minmax(0,1fr)] overflow-hidden">
      <Sidebar />
      <Outlet />
    </div>
  )
}

// The paths the server answers with this app (packs/shared/web/app.py PAGE_PATHS). /searches
// is a layout route, so picking a run (/searches/run?run_id=…) keeps the page mounted. Setup
// (/install) sits in the shell too, so the other pages are a click away while it runs. The
// review flow at / stands outside the shell: it is the user's entry point, with its own top
// bar. Any other client-side path lands on HOME.
export function App() {
  // The desktop app has no browser bar to leave the review by: there it keeps the side nav.
  const desktop = isDesktop()
  return (
    <BrowserRouter>
      <Routes>
        {desktop ? null : <Route path="/" element={<ReviewPage />} />}
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
          <Route path="/searches" element={<SearchesPage />}>
            <Route index element={null} />
            <Route path="run" element={null} />
          </Route>
          <Route path="/sets" element={<SetsPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="*" element={<Navigate to={HOME.href} replace />} />
        </Route>
      </Routes>
      <SignInModal />
      <DebugMenu />
    </BrowserRouter>
  )
}
