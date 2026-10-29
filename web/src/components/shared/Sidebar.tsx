import { useEffect, useState, type ComponentType, type SVGProps } from "react"
import { NavLink } from "react-router-dom"

import { isDesktop } from "@/lib/desktop"
import { useFeatures } from "@/lib/features"
import { useQuery } from "@tanstack/react-query"

import { ACCOUNT_KEY, fetchAccount } from "@/lib/api/account"
import { fetchInstall } from "@/lib/api/install"
import { PAGES, type Page, type PageKey } from "@/lib/nav"
import { COLLAPSED_KEY } from "@/lib/sidebar"
import { readStored, writeStored } from "@/lib/storage"
import { useSetupDone } from "@/hooks/useSetupDone"
import { cn } from "@/lib/utils"
import { Chats } from "@/pages/agent/Chats"

import {
  ChatIcon,
  CollapseIcon,
  MemoryIcon,
  PeopleIcon,
  SearchesIcon,
  SettingsIcon,
  SetupIcon,
  TasksIcon,
  TeamIcon,
} from "./icons/nav"
import { HiveIcon } from "./icons/actions"
import { CircleUserRound } from "lucide-react"
import { PowersetMark } from "./PowersetMark"
import { Spinner } from "./Spinner"
import { SidebarFooter } from "./SidebarFooter"

// Below this width the nav is always the icon rail; the pages' own panels need the room.
const RAIL_ONLY_QUERY = "(max-width: 900px)"

const ICONS: Record<PageKey, ComponentType<SVGProps<SVGSVGElement>>> = {
  home: SearchesIcon,
  agent: ChatIcon,
  searches: SearchesIcon,
  people: PeopleIcon,
  memory: MemoryIcon,
  sets: HiveIcon,
  team: TeamIcon,
  tasks: TasksIcon,
  setup: SetupIcon,
  settings: SettingsIcon,
  accounts: CircleUserRound,
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

/** The app's side nav: the brand with the collapse toggle, one item per page (Setup only until
 *  it is done; Settings then offers Repair setup), the desktop app's chats, and at the bottom the
 *  update pane and who is signed in. Collapsed it is an icon rail with the labels as tooltips;
 *  the choice is remembered on this machine. */
export function Sidebar() {
  const features = useFeatures()
  // The Team page is the Powerset team's: shown to a signed-in @powerset.co account.
  const account = useQuery({ queryKey: ACCOUNT_KEY, queryFn: ({ signal }) => fetchAccount(signal) })
  const onTeam = account.data?.email?.toLowerCase().endsWith("@powerset.co") ?? false
  const done = useSetupDone()
  // While setup runs, its nav item turns: the ring in place of the icon, the running step's line
  // as its tip. The same record the Setup page reads, so the two never disagree.
  const install = useQuery({
    queryKey: ["install"],
    queryFn: ({ signal }) => fetchInstall(signal),
    enabled: isDesktop() && !done,
    refetchInterval: 5_000,
    retry: false,
  })
  const running = install.data?.status === "running" ? install.data.message : undefined
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
        // min-h-0: the window's height bounds the nav, so a long chat list scrolls inside it.
        "relative z-10 flex h-full min-h-0 flex-col border-r border-line bg-[var(--sidebar-bg)] transition-[width] duration-med ease-out",
        rail ? "w-14" : "w-[260px]",
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
        <NavLink
          to="/home"
          aria-label="Powerset home"
          className="flex min-w-0 flex-1 items-center gap-2.5 rounded-[var(--radius-s)]"
        >
          <PowersetMark className="size-7 rounded-[7px] shadow-[var(--shadow-1)]" />
          {!rail && (
            <span className="min-w-0 flex-1 truncate text-[12px] font-extrabold tracking-[.14em] text-foreground">
              POWER<span className="text-primary">SET</span>
            </span>
          )}
        </NavLink>
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
      <ul className="m-0 flex shrink-0 list-none flex-col gap-0.5 p-2">
        {PAGES.filter(
          (page) =>
            (page.key !== "sets" || features.sets) &&
            (page.key !== "team" || (onTeam && features.team)) &&
            (page.key !== "setup" || !done),
        ).map((page) => (
          <li key={page.key}>
            <Item page={page} rail={rail} running={page.key === "setup" ? running : undefined} />
          </li>
        ))}
      </ul>
      {isDesktop() ? <Chats rail={rail} /> : null}
      <SidebarFooter rail={rail} />
    </nav>
  )
}

function Item({ page, rail, running }: { page: Page; rail: boolean; running?: string }) {
  const Icon = ICONS[page.key]
  return (
    <NavLink
      to={page.href}
      data-nav={page.key}
      title={running ?? (rail ? page.label : undefined)}
      className={cn(
        "flex h-9 items-center gap-2.5 rounded-[var(--radius-s)] px-2.5 text-[13px] font-medium text-muted-foreground no-underline transition-colors duration-fast ease-out hover:bg-secondary hover:text-foreground",
        "aria-[current=page]:bg-surface-2 aria-[current=page]:font-semibold aria-[current=page]:text-foreground",
        rail && "justify-center px-0",
      )}
    >
      {running ? (
        <span className="grid size-[18px] shrink-0 place-items-center">
          <Spinner className="size-3.5 text-primary" />
        </span>
      ) : (
        <Icon className="size-[18px] shrink-0" />
      )}
      {!rail && <span className="truncate">{page.label}</span>}
    </NavLink>
  )
}
