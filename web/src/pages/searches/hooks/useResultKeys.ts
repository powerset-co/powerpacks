import { useEffect, useRef } from "react"

export interface ResultKeyActions {
  move: (step: 1 | -1) => void
  // Each acts on the focused row and says whether there was one.
  toggle: () => boolean
  tag: () => boolean
  score: () => boolean
}

// j/k move; the arrows stay the sidebar's (hooks/useSidebarKeys).
const STEPS: Readonly<Record<string, 1 | -1>> = { j: 1, k: -1 }

/**
 * The results' keys: j/k move the focused row, Enter opens or closes it, t opens its tag
 * editor, s its score dialog. Typing in a field, a modifier, or an open dialog or tag panel
 * leaves the keys alone; a focused button or link keeps its own Enter.
 */
export function useResultKeys(actions: ResultKeyActions) {
  const latest = useRef(actions)
  useEffect(() => {
    latest.current = actions
  })

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || event.repeat) return
      const target = event.target instanceof Element ? event.target : document.body
      if (target.matches("input, select, textarea") || target.closest("[role=dialog]")) return
      const run = latest.current
      const step = STEPS[event.key]
      if (step !== undefined) {
        event.preventDefault()
        run.move(step)
        return
      }
      const act =
        event.key === "Enter" && !target.matches("button, a, summary")
          ? run.toggle
          : event.key === "t"
            ? run.tag
            : event.key === "s"
              ? run.score
              : null
      if (act?.()) event.preventDefault()
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [])
}
