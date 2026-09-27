import { useEffect, useRef } from "react"

export interface SidebarKeyActions {
  focusSearch: () => void
  move: (step: 1 | -1) => void
  open: () => void
}

// j/k belong to the results (hooks/useResultKeys).
const STEPS: Readonly<Record<string, 1 | -1>> = { ArrowDown: 1, ArrowUp: -1 }

// Text entry: the search box and the two selects.
function typing(target: Element): boolean {
  return target.matches("input, select, textarea")
}

/**
 * The list's keys (results.js catalog block): / focuses the search; the arrows move the
 * highlight; Enter opens it. Arrows and Enter also work from the search box, so a typed filter
 * goes straight to a run; Escape leaves a field.
 */
export function useSidebarKeys(actions: SidebarKeyActions) {
  const latest = useRef(actions)
  useEffect(() => {
    latest.current = actions
  })

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target instanceof Element ? event.target : document.body
      const inField = typing(target)
      const inSearch = target.matches("input[type=search]")
      const run = latest.current
      if (inField && event.key === "Escape" && target instanceof HTMLElement) {
        target.blur()
        return
      }
      if (inField && !inSearch) return
      const step = STEPS[event.key]
      if (step !== undefined) {
        event.preventDefault()
        run.move(step)
      } else if (event.key === "Enter" && !target.matches("button, a, summary")) {
        run.open()
      } else if (event.key === "/" && !inField) {
        event.preventDefault()
        run.focusSearch()
      }
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [])
}
