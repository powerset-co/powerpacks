import { useEffect, useRef } from "react";

export interface KeyboardActions {
  focusSearch: () => void;
  focusFacets: () => void;
  switchTab: (position: 0 | 1 | 2) => void;
  move: (step: 1 | -1) => void;
  toggleFocused: () => boolean;
  selectAll: () => void;
  share: () => void;
  keepPrivate: () => void;
  useWorth: () => void;
  undo: () => void;
  openFocused: () => void;
  escape: () => void;
}

// Keys whose action should not fire again while held down.
const NO_REPEAT = new Set(["s", "p", "w", "z", "x", " ", "Enter"]);
const TABS: Record<string, 0 | 1 | 2> = { "1": 0, "2": 1, "3": 2 };

// Text entry, not a checkbox: Escape on a focused checkbox still clears the selection.
function typing(target: Element): boolean {
  return target.matches("input:not([type=checkbox]), select, textarea");
}

/** The page's shortcuts. Typing in a field only honours Escape (it leaves the field). */
export function useKeyboard(actions: KeyboardActions) {
  const latest = useRef(actions);
  latest.current = actions;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : document.body;
      const run = latest.current;
      if (typing(target) || event.metaKey || event.ctrlKey || event.altKey) {
        if (event.key === "Escape" && target instanceof HTMLElement) target.blur();
        return;
      }
      // A focused button, link, summary, label or checkbox keeps its native Enter and Space.
      if ((event.key === "Enter" || event.key === " ") && target.matches("button, a, summary, label, input")) return;
      if (event.repeat && NO_REPEAT.has(event.key)) return;
      const tab = TABS[event.key];
      if (tab !== undefined) return run.switchTab(tab);
      switch (event.key) {
        case "/": event.preventDefault(); run.focusSearch(); break;
        case "f": event.preventDefault(); run.focusFacets(); break;
        case "j": case "ArrowDown": event.preventDefault(); run.move(1); break;
        case "k": case "ArrowUp": event.preventDefault(); run.move(-1); break;
        case "x": case " ": if (run.toggleFocused()) event.preventDefault(); break;
        case "A": if (event.shiftKey) { event.preventDefault(); run.selectAll(); } break;
        case "s": run.share(); break;
        case "p": run.keepPrivate(); break;
        case "w": run.useWorth(); break;
        case "z": run.undo(); break;
        case "Enter": run.openFocused(); break;
        case "Escape": run.escape(); break;
        default: break;
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
}
