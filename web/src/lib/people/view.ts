// The People view (tab, facet selections, search text, sort) in query parameters.

import { WORTH_TABS, type WorthTab } from "@/types/people"

import { FACETS, HIDES, isSortKey, type FacetFilters, type HideKey, type Sort } from "./facets"

/** The Worth select: everyone, one worth call, or no call yet. */
export type Tab = WorthTab
export const TABS: readonly Tab[] = WORTH_TABS

export interface PeopleView {
  tab: Tab
  filters: FacetFilters
  // The Filters menu: the hides the owner turned on (true, `hide=`) or off (false, `show=`). One left
  // alone is the server's call: on while anyone else would show (the page's `hidden`).
  hide: ReadonlyMap<HideKey, boolean>
  text: string
  sort: Sort
}

export const DEFAULT_VIEW: PeopleView = {
  tab: "all",
  filters: new Map(),
  hide: new Map(),
  text: "",
  // Warmest first: warmth sorts warmest-first in its ascending direction, like the column's first click.
  sort: { key: "warmth", dir: 1 },
}

export function readView(params: URLSearchParams): PeopleView {
  const key = params.get("sort")
  return {
    tab: TABS.find((tab) => tab === params.get("tab")) ?? DEFAULT_VIEW.tab,
    text: params.get("q") ?? "",
    sort: { key: isSortKey(key) ? key : DEFAULT_VIEW.sort.key, dir: params.get("dir") === "desc" ? -1 : 1 },
    filters: new Map(
      FACETS.flatMap(({ key }) => {
        const values = params.getAll(`filter.${key}`)
        return values.length ? [[key, new Set(values)]] : []
      }),
    ),
    hide: new Map(
      HIDES.flatMap(({ key }): [HideKey, boolean][] =>
        params.getAll("show").includes(key)
          ? [[key, false]]
          : params.getAll("hide").includes(key)
            ? [[key, true]]
            : [],
      ),
    ),
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
  if (sort.key !== DEFAULT_VIEW.sort.key) params.set("sort", sort.key)
  else params.delete("sort")
  if (sort.dir === -1) params.set("dir", "desc")
  else params.delete("dir")
  for (const { key } of FACETS) {
    params.delete(`filter.${key}`)
    for (const value of filters.get(key) ?? []) params.append(`filter.${key}`, value)
  }
  params.delete("show")
  params.delete("hide")
  for (const [key, on] of hide) params.append(on ? "hide" : "show", key)
  return params
}

/** The view as the server's list reads it (GET /api/people/page): the same parameters as the URL. */
export function viewQuery(view: PeopleView): string {
  return writeView(view, new URLSearchParams()).toString()
}
