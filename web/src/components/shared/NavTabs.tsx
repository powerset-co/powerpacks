import { NavLink, useLocation } from "react-router-dom"

import { must } from "@/lib/must"
import { PAGES, pageAt, type PageKey } from "@/lib/nav"

import { TabInk } from "./TabInk"

const TAB =
  "rounded-sm px-2.5 py-1.5 text-xs font-semibold text-muted-foreground no-underline transition-[color,background-color] duration-fast ease-out hover:bg-secondary hover:text-foreground aria-[current=page]:bg-surface-2 aria-[current=page]:text-foreground"

const ORDER: readonly PageKey[] = ["searches", "people"]

// results.css .topbar-nav: page tabs that start where the main pane starts. NavLink marks the
// current page with aria-current; the ink slides under it as the router switches pages.
export function NavTabs() {
  const current = pageAt(useLocation().pathname)
  const active = must(
    ORDER.find((key) => PAGES[key] === current),
    "nav tab",
  )
  return (
    <nav aria-label="Local UI" className="relative ml-2.5 inline-flex gap-0.5 justify-self-start">
      {ORDER.map((key) => (
        <NavLink key={key} to={PAGES[key].href} data-nav={key} className={TAB}>
          {PAGES[key].label}
        </NavLink>
      ))}
      <TabInk active={`[data-nav='${active}']`} />
    </nav>
  )
}
