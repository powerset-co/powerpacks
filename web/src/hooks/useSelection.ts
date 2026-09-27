import { useCallback, useMemo, useState } from "react"

import { EMPTY, toggled } from "@/lib/sets"

/**
 * Selected ids; select-all works on every matching item, not just the mounted rows.
 * `keyOf` must be stable (module scope): `toggleAll` changes whenever it does.
 */
export function useSelection<T>(matching: readonly T[], keyOf: (item: T) => string) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(EMPTY)

  const toggle = useCallback((id: string) => {
    setSelected((current) => toggled(current, id))
  }, [])

  const clear = useCallback(() => setSelected(EMPTY), [])

  const selectedHere = useMemo(
    () => matching.filter((item) => selected.has(keyOf(item))).length,
    [matching, selected, keyOf],
  )
  const allSelected = matching.length > 0 && selectedHere === matching.length

  // All matching selected already: clear; otherwise add every matching item.
  const toggleAll = useCallback(() => {
    setSelected((current) => {
      if (matching.every((item) => current.has(keyOf(item)))) return EMPTY
      const next = new Set(current)
      for (const item of matching) next.add(keyOf(item))
      return next
    })
  }, [matching, keyOf])

  return { selected, toggle, toggleAll, clear, allSelected, someSelected: selectedHere > 0 && !allSelected }
}
