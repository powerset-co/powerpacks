// The local UI's pages, in top-bar order. The review server serves each at its href.

export type PageKey = "searches" | "people"

export interface Page {
  key: PageKey
  label: string
  href: string
}

/** Where an unknown path lands. */
export const HOME: Page = { key: "people", label: "People", href: "/people" }

export const PAGES: readonly Page[] = [{ key: "searches", label: "Searches", href: "/searches" }, HOME]

/** The page a routed path belongs to: its href or a path under it; any other path is HOME,
 *  which the router's catch-all redirects to. */
export function pageAt(pathname: string): Page {
  return PAGES.find(({ href }) => pathname === href || pathname.startsWith(`${href}/`)) ?? HOME
}
