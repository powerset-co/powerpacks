import { useCallback, useState } from "react"

import { EMPTY, toggled } from "@/lib/sets"

/**
 * Selected ids out of a list of `count` matching items that is read a slice at a time: select-all
 * asks `allIds` for every matching id, not just the loaded rows. The owner clears the selection
 * when the list changes (a new tab, filter or search), so a selection is always within it.
 */
export function useSelection(count: number, allIds: () => Promise<readonly string[]>) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(EMPTY)

  const toggle = useCallback((id: string) => {
    setSelected((current) => toggled(current, id))
  }, [])

  const clear = useCallback(() => setSelected(EMPTY), [])

  const allSelected = count > 0 && selected.size >= count

  // All matching selected already: clear; otherwise every matching id.
  const toggleAll = useCallback(() => {
    if (allSelected) {
      setSelected(EMPTY)
      return
    }
    void allIds().then((ids) => setSelected(new Set(ids)))
  }, [allSelected, allIds])

  return { selected, toggle, toggleAll, clear, allSelected, someSelected: selected.size > 0 && !allSelected }
}
