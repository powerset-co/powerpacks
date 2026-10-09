import { useEffect, useState, type ComponentType, type SVGProps } from "react"
import { NavLink } from "react-router-dom"

import { PAGES, type Page, type PageKey } from "@/lib/nav"
import { readStored, writeStored } from "@/lib/storage"
import { cn } from "@/lib/utils"

import { AccountsIcon, ChatIcon, CollapseIcon, PeopleIcon, SearchesIcon, TasksIcon } from "./icons/nav"

const COLLAPSED_KEY = "sidebar.collapsed"
// Below this width the nav is always the icon rail; the pages' own panels need the room.
const RAIL_ONLY_QUERY = "(max-width: 900px)"

const ICONS: Record<PageKey, ComponentType<SVGProps<SVGSVGElement>>> = {
  agent: ChatIcon,
  searches: SearchesIcon,
  people: PeopleIcon,
  tasks: TasksIcon,
  accounts: AccountsIcon,
}

function useMedia(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const media = window.matchMedia(query)
    const update = () => setMatches(media.matches)
    media.addEventListener("change", update)
    return () => media.removeEventListener("change", update)
  }, [query])
  return matches
}

/** The app's side nav: the brand, one item per page, and a collapse toggle. Collapsed it is an
 *  icon rail with the labels as tooltips; the choice is remembered on this machine. */
export function Sidebar() {
  const [collapsed, setCollapsed] = useState(
    () => readStored("local", COLLAPSED_KEY, (raw) => (typeof raw === "boolean" ? raw : null)) ?? false,
  )
  const railOnly = useMedia(RAIL_ONLY_QUERY)
  const rail = collapsed || railOnly
  const toggle = () => {
    setCollapsed(!collapsed)
    writeStored("local", COLLAPSED_KEY, !collapsed)
  }
  return (
    <nav
      aria-label="Pages"
      data-collapsed={rail}
      className={cn(
        "flex h-full flex-col border-r border-line bg-[color-mix(in_srgb,var(--card)_55%,var(--background))] transition-[width] duration-med ease-out",
        rail ? "w-14" : "w-[220px]",
      )}
    >
      <div
        data-tauri-drag-region
        data-sidebar-brand
        className={cn("flex h-topbar shrink-0 items-center gap-2.5 px-4", rail && "justify-center px-0")}
      >
        <span
          aria-hidden
          className="size-2.5 shrink-0 rounded-[3px] bg-primary shadow-[0_0_0_3px_var(--primary-soft)]"
        />
        {!rail && (
          <span className="text-[11px] font-extrabold tracking-[.16em] text-foreground">POWERPACKS</span>
        )}
      </div>
      <ul className="m-0 flex list-none flex-col gap-0.5 p-2">
        {PAGES.map((page) => (
          <li key={page.key}>
            <Item page={page} rail={rail} />
          </li>
        ))}
      </ul>
      <div className="mt-auto p-2">
        <button
          type="button"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!rail}
          title={collapsed ? "Expand" : "Collapse"}
          onClick={toggle}
          disabled={railOnly}
          className={cn(
            "flex h-9 w-full cursor-pointer items-center gap-2.5 rounded-[var(--radius-s)] border-0 bg-transparent px-2.5 text-xs text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground disabled:cursor-default disabled:opacity-40",
            rail && "justify-center px-0",
          )}
        >
          <CollapseIcon className={cn("size-[18px] shrink-0", collapsed && "-scale-x-100")} />
          {!rail && "Collapse"}
        </button>
      </div>
    </nav>
  )
}

function Item({ page, rail }: { page: Page; rail: boolean }) {
  const Icon = ICONS[page.key]
  return (
    <NavLink
      to={page.href}
      data-nav={page.key}
      title={rail ? page.label : undefined}
      className={cn(
        "flex h-9 items-center gap-2.5 rounded-[var(--radius-s)] px-2.5 text-[13px] font-medium text-muted-foreground no-underline transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground",
        "aria-[current=page]:bg-surface-2 aria-[current=page]:font-semibold aria-[current=page]:text-foreground",
        rail && "justify-center px-0",
      )}
    >
      <Icon className="size-[18px] shrink-0" />
      {!rail && <span className="truncate">{page.label}</span>}
    </NavLink>
  )
}
