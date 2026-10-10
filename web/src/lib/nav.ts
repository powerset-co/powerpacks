// The local UI's pages, in side-nav order. The review server serves each at its href.

import { isDesktop } from "@/lib/desktop"

export type PageKey = "home" | "agent" | "searches" | "people" | "sets" | "accounts" | "tasks" | "setup"

export interface Page {
  key: PageKey
  label: string
  href: string
}

/** Where an unknown path lands. */
export const HOME: Page = { key: "people", label: "People", href: "/people" }

const BROWSER_PAGES: readonly Page[] = [
  { key: "searches", label: "Searches", href: "/searches" },
  HOME,
  { key: "sets", label: "Sets", href: "/sets" },
  { key: "tasks", label: "Scheduled tasks", href: "/tasks" },
  { key: "accounts", label: "Accounts", href: "/accounts" },
  // Setup runs for a while; it sits in the nav so the rest of the app is a click away.
  { key: "setup", label: "Setup", href: "/install" },
]

/** The desktop app's one search page: chat with the Codex agent beside the saved searches and
 *  the run it opened. It replaces the browser's Searches page there. */
export const AGENT: Page = { key: "agent", label: "Searches", href: "/agent" }

export const PAGES: readonly Page[] = isDesktop()
  ? [AGENT, ...BROWSER_PAGES.filter(({ key }) => key !== "searches")]
  : BROWSER_PAGES

/** The page a routed path belongs to: its href or a path under it; any other path is HOME,
 *  which the router's catch-all redirects to. */
export function pageAt(pathname: string): Page {
  if (pathname === "/home") return { key: "home", label: "Home", href: "/home" }
  return PAGES.find(({ href }) => pathname === href || pathname.startsWith(`${href}/`)) ?? HOME
}
