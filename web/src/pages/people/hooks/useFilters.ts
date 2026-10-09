import { useCallback, useState } from "react"

import { setEntries, type FacetKey, type FacetSet, type SortKey } from "@/lib/people/facets"
import { DEFAULT_VIEW, readView, type PeopleView } from "@/lib/people/view"
import { EMPTY, toggled } from "@/lib/sets"
import { ORDER, type Decision, type Person } from "@/types/people"

// Honor a linked tab; otherwise start at the first decision with people.
function startView(rows: readonly Person[], params: URLSearchParams): PeopleView {
  const view = readView(params)
  if (params.has("tab") || rows.some((row) => row.share === view.tab)) return view
  const tab = ORDER.find((decision) => rows.some((row) => row.share === decision)) ?? DEFAULT_VIEW.tab
  return { ...view, tab }
}

/** Tab, facet selections, search text and sort, initialized from the URL. */
export function useFilters(rows: readonly Person[], params: URLSearchParams) {
  const [view, setView] = useState<PeopleView>(() => startView(rows, params))

  const setTab = useCallback((tab: Decision) => {
    setView((current) => (current.tab === tab ? current : { ...current, tab }))
  }, [])

  const toggleFilter = useCallback((key: FacetKey, value: string) => {
    setView((current) => {
      const filters = new Map(current.filters)
      const held = toggled(filters.get(key) ?? EMPTY, value)
      if (held.size) filters.set(key, held)
      else filters.delete(key)
      return { ...current, filters }
    })
  }, [])

  const setFilters = useCallback((set: FacetSet) => {
    const filters = new Map(setEntries(set).map(([key, values]) => [key, new Set(values)]))
    setView((current) => ({ ...current, filters }))
  }, [])

  const setText = useCallback((text: string) => setView((current) => ({ ...current, text })), [])

  // The same column flips direction; a new column starts ascending.
  const setSort = useCallback((key: SortKey) => {
    setView((current) => {
      const dir = current.sort.key === key ? (current.sort.dir === 1 ? -1 : 1) : 1
      return { ...current, sort: { key, dir } }
    })
  }, [])

  return { view, setTab, toggleFilter, setFilters, setText, setSort }
}
