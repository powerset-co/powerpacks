import type { CSSProperties, ReactNode } from "react"

import { ROW_H } from "./table/columns"
import "./styles/shell.css"

// The table's row height is a People concern: table/columns.ts ROW_H drives --row-h here.
const ROW_HEIGHT: CSSProperties = { "--row-h": `${ROW_H}px` }

interface PeopleShellProps {
  main: ReactNode
  overlays?: ReactNode
}

// The app shell's second row: the main pane; the drawer, bulk bar and toast float over it.
export function PeopleShell({ main, overlays }: PeopleShellProps) {
  return (
    <div className="people-shell" data-people style={ROW_HEIGHT}>
      <main className="people-main">{main}</main>
      {overlays}
    </div>
  )
}
