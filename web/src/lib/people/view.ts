// The People view (tab, facet selections, search text, sort) in query parameters.

import { ORDER, type Decision } from "@/types/people"

import { FACETS, isSortKey, type FacetFilters, type Sort } from "./facets"

export interface PeopleView {
  tab: Decision
  filters: FacetFilters
  text: string
  sort: Sort
}

export const DEFAULT_VIEW: PeopleView = {
  tab: "confirm",
  filters: new Map(),
  text: "",
  sort: { key: "name", dir: 1 },
}

export function readView(params: URLSearchParams): PeopleView {
  const key = params.get("sort")
  return {
    tab: ORDER.find((tab) => tab === params.get("tab")) ?? DEFAULT_VIEW.tab,
    text: params.get("q") ?? "",
    sort: { key: isSortKey(key) ? key : "name", dir: params.get("dir") === "desc" ? -1 : 1 },
    filters: new Map(
      FACETS.flatMap(({ key }) => {
        const values = params.getAll(`filter.${key}`)
        return values.length ? [[key, new Set(values)]] : []
      }),
    ),
  }
}

export function writeView(
  { tab, filters, text, sort }: PeopleView,
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
  return params
}
