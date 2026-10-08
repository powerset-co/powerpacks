// The local UI's pages, in top-bar order. The review server serves each at its href.

import { isDesktop } from "@/lib/desktop"

export type PageKey = "agent" | "searches" | "people" | "accounts" | "tasks"

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
  { key: "tasks", label: "Scheduled tasks", href: "/tasks" },
  { key: "accounts", label: "Accounts", href: "/accounts" },
]

/** Chat with the Codex agent; only the desktop app can run it, so only the desktop app shows it. */
export const AGENT: Page = { key: "agent", label: "Chat", href: "/agent" }

export const PAGES: readonly Page[] = isDesktop() ? [AGENT, ...BROWSER_PAGES] : BROWSER_PAGES

/** The page a routed path belongs to: its href or a path under it; any other path is HOME,
 *  which the router's catch-all redirects to. */
export function pageAt(pathname: string): Page {
  return PAGES.find(({ href }) => pathname === href || pathname.startsWith(`${href}/`)) ?? HOME
}
