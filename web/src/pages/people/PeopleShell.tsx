import type { CSSProperties, ReactNode } from "react"

import { ROW_H } from "./table/columns"
import "./styles/shell.css"
import "./styles/rail.css"

// The table's row height is a People concern: table/columns.ts ROW_H drives --row-h here.
const ROW_HEIGHT: CSSProperties = { "--row-h": `${ROW_H}px` }

interface PeopleShellProps {
  rail: ReactNode
  main: ReactNode
  overlays?: ReactNode
}

// The app shell's second row: the rail beside the main pane; the drawer, bulk bar and toast float over both.
export function PeopleShell({ rail, main, overlays }: PeopleShellProps) {
  return (
    <div className="people-shell" data-people style={ROW_HEIGHT}>
      <div className="people-layout">
        <aside className="rail" data-rail aria-label="Filters">
          {rail}
        </aside>
        <main className="people-main">{main}</main>
      </div>
      {overlays}
    </div>
  )
}
