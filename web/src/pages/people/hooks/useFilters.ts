import { useCallback, useState } from "react"

import { setEntries, type FacetSet, type HideKey, type SortKey } from "@/lib/people/facets"
import { readView, type PeopleView, type Tab } from "@/lib/people/view"

/** Tab, facet selections, search text and sort, initialized from the URL (All when it names none). */
export function useFilters(params: URLSearchParams) {
  const [view, setView] = useState<PeopleView>(() => readView(params))

  const setTab = useCallback((tab: Tab) => {
    setView((current) => (current.tab === tab ? current : { ...current, tab }))
  }, [])

  const setFilters = useCallback((set: FacetSet) => {
    const filters = new Map(setEntries(set).map(([key, values]) => [key, new Set(values)]))
    setView((current) => ({ ...current, filters }))
  }, [])

  // `on`: the switch's new state, the owner's choice from now on.
  const setHide = useCallback((key: HideKey, on: boolean) => {
    setView((current) => ({ ...current, hide: new Map(current.hide).set(key, on) }))
  }, [])

  // Clear filters: every facet and every hide back to where the page starts.
  const clearFilters = useCallback(
    () => setView((current) => ({ ...current, filters: new Map(), hide: new Map() })),
    [],
  )

  const setText = useCallback((text: string) => setView((current) => ({ ...current, text })), [])

  // The same column flips direction; a new column starts ascending.
  const setSort = useCallback((key: SortKey) => {
    setView((current) => {
      const dir = current.sort.key === key ? (current.sort.dir === 1 ? -1 : 1) : 1
      return { ...current, sort: { key, dir } }
    })
  }, [])

  return { view, setTab, setFilters, setHide, clearFilters, setText, setSort }
}
