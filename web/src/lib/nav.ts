// The local UI's pages, in side-nav order. The review server serves each at its href.

import { isDesktop } from "@/lib/desktop"

export type PageKey =
  | "agent"
  | "searches"
  | "people"
  | "accounts"
  | "memory"
  | "sets"
  | "team"
  | "tasks"
  | "setup"
  | "sources"
  | "settings"

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
  { key: "accounts", label: "Accounts", href: "/accounts" },
  // Memory (/memory) is reachable but not listed: it is not ready for the nav.
  { key: "sets", label: "Sets", href: "/sets" },
  { key: "tasks", label: "Scheduled tasks", href: "/tasks" },
  // The Powerset team only (Sidebar hides it otherwise): teammates' agents and support threads.
  { key: "team", label: "Team", href: "/team" },
  // Setup runs for a while; it sits in the nav so the rest of the app is a click away. The app's
  // is the onboarding's Set up screen; the browser's is the install page.
  { key: "setup", label: "Setup", href: isDesktop() ? "/onboarding/setup" : "/install" },
  // Once setup is done with LinkedIn alone: the onboarding's Sources screen, for Messages, Gmail
  // and WhatsApp. Gone once a message source is in.
  { key: "sources", label: "Add sources", href: "/onboarding/sources" },
]

/** The desktop app's one search page: chat with the Codex agent beside the saved searches and
 *  the run it opened. It replaces the browser's Searches page there, and is not a nav item: the
 *  side nav's chats section is headed Searches, and New chat and the chats open it. */
export const AGENT: Page = { key: "agent", label: "Searches", href: "/agent" }

export const PAGES: readonly Page[] = isDesktop()
  ? BROWSER_PAGES.filter(({ key }) => key !== "searches")
  : BROWSER_PAGES

/** The page a routed path belongs to: its href or a path under it; any other path is HOME,
 *  which the router's catch-all redirects to. */
export function pageAt(pathname: string): Page {
  // Out of the side nav for now; the debug menu opens it.
  if (pathname === "/settings") return { key: "settings", label: "Settings", href: "/settings" }
  if (pathname === AGENT.href || pathname.startsWith(`${AGENT.href}/`)) return AGENT
  return PAGES.find(({ href }) => pathname === href || pathname.startsWith(`${href}/`)) ?? HOME
}
