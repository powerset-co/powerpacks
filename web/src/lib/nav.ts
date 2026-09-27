// The local UI's pages, in top-bar order. The review server serves each at its href.

import { must } from "@/lib/must"

export type PageKey = "searches" | "people"

export interface Page {
  label: string
  href: string
}

export const PAGES: Readonly<Record<PageKey, Page>> = {
  searches: { label: "Searches", href: "/searches" },
  people: { label: "People", href: "/people" },
}

/** The page a routed path belongs to: its href, or a path under it. */
export function pageAt(pathname: string): Page {
  const page = Object.values(PAGES).find(({ href }) => pathname === href || pathname.startsWith(`${href}/`))
  return must(page, `page for ${pathname}`)
}
