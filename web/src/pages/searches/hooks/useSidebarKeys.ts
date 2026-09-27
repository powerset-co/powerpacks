import { isTyping, useKeys } from "@/hooks/useKeys"

export interface SidebarKeyActions {
  focusSearch: () => void
  move: (step: 1 | -1) => void
  open: () => void
}

// j/k belong to the results (hooks/useResultKeys).
const STEPS: Readonly<Record<string, 1 | -1>> = { ArrowDown: 1, ArrowUp: -1 }

/**
 * The list's keys (results.js catalog block): / focuses the search; the arrows move the
 * highlight; Enter opens it. Arrows and Enter also work from the search box, so a typed filter
 * goes straight to a run; Escape leaves a field.
 */
export function useSidebarKeys(run: SidebarKeyActions) {
  useKeys((event, target) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return
    const inField = isTyping(target)
    const inSearch = target.matches("input[type=search]")
    if (inField && event.key === "Escape" && target instanceof HTMLElement) {
      target.blur()
      return
    }
    if (inField && !inSearch) return
    const step = STEPS[event.key]
    if (step !== undefined) {
      event.preventDefault()
      run.move(step)
    } else if (event.key === "Enter" && !target.matches("button, a")) {
      run.open()
    } else if (event.key === "/" && !inField) {
      event.preventDefault()
      run.focusSearch()
    }
  })
}
