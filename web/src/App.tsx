import { useEffect } from "react"
import { BrowserRouter, Outlet, Route, Routes, useLocation } from "react-router-dom"

import { TopBar } from "@/components/shared"
import { pageAt } from "@/lib/nav"
import { PeoplePage } from "@/pages/people/PeoplePage"

// The top bar row, then the page: the page owns its own scrolling inside the second row.
function Shell() {
  const page = pageAt(useLocation().pathname)
  useEffect(() => {
    document.title = `${page.label} · Powerpacks`
  }, [page])
  return (
    <div className="grid h-dvh grid-rows-[var(--topbar-height)_1fr] overflow-hidden">
      <TopBar />
      <Outlet />
    </div>
  )
}

// The router claims only /people; /searches is still the server-rendered page.
export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route path="/people" element={<PeoplePage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
