import { isTyping, useKeys } from "@/hooks/useKeys"

export interface KeyboardActions {
  focusSearch: () => void
  focusFacets: () => void
  switchTab: (position: 0 | 1 | 2) => void
  move: (step: 1 | -1) => void
  toggleFocused: () => boolean
  selectAll: () => void
  share: () => void
  keepPrivate: () => void
  useWorth: () => void
  undo: () => void
  openFocused: () => void
  escape: () => void
}

// Keys whose action should not fire again while held down.
const NO_REPEAT = new Set(["s", "p", "w", "z", "x", " ", "Enter"])
const TABS: Record<string, 0 | 1 | 2> = { "1": 0, "2": 1, "3": 2 }

/** The page's shortcuts. Typing in a field only honours Escape (it leaves the field). */
export function useKeyboard(run: KeyboardActions) {
  useKeys((event, target) => {
    if (isTyping(target) || event.metaKey || event.ctrlKey || event.altKey) {
      if (event.key === "Escape" && target instanceof HTMLElement) target.blur()
      return
    }
    // A focused button, link, label or checkbox keeps its native Enter and Space.
    if ((event.key === "Enter" || event.key === " ") && target.matches("button, a, label, input")) return
    if (event.repeat && NO_REPEAT.has(event.key)) return
    const tab = TABS[event.key]
    if (tab !== undefined) {
      run.switchTab(tab)
      return
    }
    switch (event.key) {
      case "/":
        event.preventDefault()
        run.focusSearch()
        break
      case "f":
        event.preventDefault()
        run.focusFacets()
        break
      case "j":
      case "ArrowDown":
        event.preventDefault()
        run.move(1)
        break
      case "k":
      case "ArrowUp":
        event.preventDefault()
        run.move(-1)
        break
      case "x":
      case " ":
        if (run.toggleFocused()) event.preventDefault()
        break
      case "A":
        if (event.shiftKey) {
          event.preventDefault()
          run.selectAll()
        }
        break
      case "s":
        run.share()
        break
      case "p":
        run.keepPrivate()
        break
      case "w":
        run.useWorth()
        break
      case "z":
        run.undo()
        break
      case "Enter":
        run.openFocused()
        break
      case "Escape":
        run.escape()
        break
      default:
        break
    }
  })
}
