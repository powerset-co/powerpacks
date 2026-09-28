import { isTyping, useKeys } from "@/hooks/useKeys"

export interface ResultKeyActions {
  move: (step: 1 | -1) => void
  // Each acts on the focused row (the open one while the drawer is open) and says whether there was one.
  toggle: () => boolean
  tag: () => boolean
  score: () => boolean
  pin: () => boolean
  // A rubric digit: scores the open candidate; false when the digit is no score or none is open.
  rate: (digit: string) => boolean
  // Closes the drawer; false when it was shut.
  close: () => boolean
}

// j/k move; the arrows stay the sidebar's (hooks/useSidebarKeys).
const STEPS: Readonly<Record<string, 1 | -1>> = { j: 1, k: -1 }

/**
 * The results' keys: j/k move the focused row (the open drawer follows), Enter opens or closes
 * the drawer on it, Escape closes it, a rubric digit scores the open candidate, t opens the tag
 * editor, s the score dialog, p toggles the pin. Typing in a field, a modifier, a held key, or
 * an open dialog or tag panel leaves the keys alone; a focused button or link keeps its own Enter.
 */
export function useResultKeys(run: ResultKeyActions) {
  useKeys((event, target) => {
    if (event.metaKey || event.ctrlKey || event.altKey || event.repeat) return
    if (isTyping(target) || target.closest("[role=dialog]")) return
    const step = STEPS[event.key]
    if (step !== undefined) {
      event.preventDefault()
      run.move(step)
      return
    }
    if (actOn(run, event.key, target)) event.preventDefault()
  })
}

function actOn(run: ResultKeyActions, key: string, target: Element): boolean {
  switch (key) {
    case "Enter":
      return !target.matches("button, a") && run.toggle()
    case "Escape":
      return run.close()
    case "t":
      return run.tag()
    case "s":
      return run.score()
    case "p":
      return run.pin()
    default:
      return run.rate(key)
  }
}
