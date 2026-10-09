import { useEffect, useState, type ComponentType, type SVGProps } from "react"
import { NavLink } from "react-router-dom"

import { PAGES, type Page, type PageKey } from "@/lib/nav"
import { readStored, writeStored } from "@/lib/storage"
import { cn } from "@/lib/utils"

import {
  AccountsIcon,
  ChatIcon,
  CollapseIcon,
  PeopleIcon,
  SearchesIcon,
  SetupIcon,
  TasksIcon,
} from "./icons/nav"
import { HiveIcon } from "./icons/actions"
import { PowersetMark } from "./PowersetMark"
import { SidebarFooter } from "./SidebarFooter"

const COLLAPSED_KEY = "sidebar.collapsed"
// Below this width the nav is always the icon rail; the pages' own panels need the room.
const RAIL_ONLY_QUERY = "(max-width: 900px)"

const ICONS: Record<PageKey, ComponentType<SVGProps<SVGSVGElement>>> = {
  agent: ChatIcon,
  searches: SearchesIcon,
  people: PeopleIcon,
  sets: HiveIcon,
  tasks: TasksIcon,
  accounts: AccountsIcon,
  setup: SetupIcon,
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

/** The app's side nav: the brand with the collapse toggle, one item per page, and at the bottom
 *  the update pane and who is signed in. Collapsed it is an icon rail with the labels as
 *  tooltips; the choice is remembered on this machine. */
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
        "relative z-10 flex h-full flex-col border-r border-line bg-[var(--sidebar-bg)] transition-[width] duration-med ease-out",
        rail ? "w-14" : "w-[220px]",
      )}
    >
      <div
        data-tauri-drag-region
        data-sidebar-brand
        className={cn(
          "flex shrink-0 items-center",
          rail ? "flex-col justify-center gap-1 px-0 pt-3 pb-1" : "h-topbar gap-2.5 pr-2 pl-4",
        )}
      >
        <PowersetMark className="size-7 rounded-[7px] shadow-[var(--shadow-1)]" />
        {!rail && (
          <span className="min-w-0 flex-1 truncate text-[12px] font-extrabold tracking-[.14em] text-foreground">
            POWER<span className="text-primary">PACKS</span>
          </span>
        )}
        <button
          type="button"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!rail}
          title={collapsed ? "Expand" : "Collapse"}
          onClick={toggle}
          disabled={railOnly}
          className="grid size-7 shrink-0 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent p-0 text-faint transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground disabled:cursor-default disabled:opacity-40"
        >
          <CollapseIcon className={cn("size-4", collapsed && "-scale-x-100")} />
        </button>
      </div>
      <ul className="m-0 flex list-none flex-col gap-0.5 p-2">
        {PAGES.map((page) => (
          <li key={page.key}>
            <Item page={page} rail={rail} />
          </li>
        ))}
      </ul>
      <SidebarFooter rail={rail} />
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
