import type { ReactNode } from "react"

import "./styles/shell.css"
import "./styles/sidebar.css"
import "./styles/run.css"
import "./styles/results.css"

interface SearchesShellProps {
  sidebar: ReactNode
  main: ReactNode
}

// The app shell's second row: the saved searches beside the open run, columns lined up with People's.
export function SearchesShell({ sidebar, main }: SearchesShellProps) {
  return (
    <div className="searches-shell" data-searches>
      <aside className="searches-rail" data-search-list aria-label="Saved searches">
        {sidebar}
      </aside>
      <main className="searches-main">{main}</main>
    </div>
  )
}
