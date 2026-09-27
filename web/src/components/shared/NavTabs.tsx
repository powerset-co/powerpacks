import { NavLink } from "react-router-dom"

import { PAGES, type Page } from "@/lib/nav"

import { TabInk } from "./TabInk"

const TAB =
  "rounded-sm px-2.5 py-1.5 text-xs font-semibold text-muted-foreground no-underline transition-[color,background-color] duration-fast ease-out hover:bg-secondary hover:text-foreground aria-[current=page]:bg-surface-2 aria-[current=page]:text-foreground"

interface NavTabsProps {
  current: Page
}

// results.css .topbar-nav: page tabs that start where the main pane starts. NavLink marks the
// current page with aria-current; the ink slides under it as the router switches pages.
export function NavTabs({ current }: NavTabsProps) {
  return (
    <nav aria-label="Local UI" className="relative ml-2.5 inline-flex gap-0.5 justify-self-start">
      {PAGES.map(({ key, label, href }) => (
        <NavLink key={key} to={href} data-nav={key} className={TAB}>
          {label}
        </NavLink>
      ))}
      <TabInk active={`[data-nav='${current.key}']`} />
    </nav>
  )
}
