// The People view (tab, facet selections, search text, sort) in query parameters.

import { WORTH_TABS, type WorthTab } from "@/types/people"

import { FACETS, HIDES, isSortKey, type FacetFilters, type HideKey, type Sort } from "./facets"

/** The Worth select: everyone, one worth call, or no call yet. */
export type Tab = WorthTab
export const TABS: readonly Tab[] = WORTH_TABS

export interface PeopleView {
  tab: Tab
  filters: FacetFilters
  // The Filters menu: the kinds of people hidden from the list. Every kind starts hidden; the
  // URL names the ones shown (`show=`).
  hide: ReadonlySet<HideKey>
  text: string
  sort: Sort
}

export const DEFAULT_VIEW: PeopleView = {
  tab: "all",
  filters: new Map(),
  hide: new Set(HIDES.map(({ key }) => key)),
  text: "",
  sort: { key: "name", dir: 1 },
}

export function readView(params: URLSearchParams): PeopleView {
  const key = params.get("sort")
  return {
    tab: TABS.find((tab) => tab === params.get("tab")) ?? DEFAULT_VIEW.tab,
    text: params.get("q") ?? "",
    sort: { key: isSortKey(key) ? key : "name", dir: params.get("dir") === "desc" ? -1 : 1 },
    filters: new Map(
      FACETS.flatMap(({ key }) => {
        const values = params.getAll(`filter.${key}`)
        return values.length ? [[key, new Set(values)]] : []
      }),
    ),
    hide: new Set(HIDES.map(({ key }) => key).filter((key) => !params.getAll("show").includes(key))),
  }
}

export function writeView(
  { tab, filters, hide, text, sort }: PeopleView,
  current: URLSearchParams,
): URLSearchParams {
  const params = new URLSearchParams(current)
  params.set("tab", tab)
  if (text) params.set("q", text)
  else params.delete("q")
  if (sort.key !== "name") params.set("sort", sort.key)
  else params.delete("sort")
  if (sort.dir === -1) params.set("dir", "desc")
  else params.delete("dir")
  for (const { key } of FACETS) {
    params.delete(`filter.${key}`)
    for (const value of filters.get(key) ?? []) params.append(`filter.${key}`, value)
  }
  params.delete("show")
  for (const { key } of HIDES) if (!hide.has(key)) params.append("show", key)
  return params
}
