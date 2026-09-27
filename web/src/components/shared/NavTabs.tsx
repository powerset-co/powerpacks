import { NavLink } from "react-router-dom"

import { PAGES } from "@/lib/nav"

const TAB =
  "rounded-sm px-2.5 py-1.5 text-xs font-semibold text-muted-foreground no-underline transition-[color,background-color] duration-fast ease-out hover:bg-secondary hover:text-foreground aria-[current=page]:bg-surface-2 aria-[current=page]:text-foreground"

// results.css .topbar-nav: page tabs that start where the main pane starts. NavLink marks the
// current page with aria-current. Searches is still the server-rendered page, outside this
// router, so its tab is a plain link that loads the document.
export function NavTabs() {
  return (
    <nav aria-label="Local UI" className="ml-2.5 inline-flex gap-0.5 justify-self-start">
      <a href={PAGES.searches.href} className={TAB}>
        {PAGES.searches.label}
      </a>
      <NavLink to={PAGES.people.href} className={TAB}>
        {PAGES.people.label}
      </NavLink>
    </nav>
  )
}
