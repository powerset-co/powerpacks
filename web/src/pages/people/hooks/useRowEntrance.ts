import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from "react";

import { useReducedMotion } from "@/hooks/useReducedMotion";
import type { PeopleView } from "@/lib/people/view";

const STAGGERED = 14;
const STAGGER_MS = 14;
const MAX_DELAY_MS = 100;

// A cancelled entrance leaves its row where the entrance would have ended.
function cancel(running: MutableRefObject<Animation[]>): void {
  running.current.forEach((animation) => animation.cancel());
  running.current = [];
}

/**
 * Rows fade and rise in after the view changes (tab, filter, search, sort): the first
 * fourteen mounted rows, 14ms apart. Scrolling, selection and writes never replay it.
 * `view` changes identity exactly when the view does. Timing is index.css's --t-med and
 * --ease-out. A new view, unmounting, or reduced motion switching on stops the entrance.
 */
export function useRowEntrance(viewport: () => HTMLElement | null, view: PeopleView, count: number) {
  const reduced = useReducedMotion();
  const played = useRef<PeopleView | null>(null);
  const running = useRef<Animation[]>([]);

  useEffect(() => {
    if (reduced) cancel(running);
  }, [reduced]);
  useEffect(() => () => cancel(running), []);

  useLayoutEffect(() => {
    if (played.current === view) return;
    cancel(running);
    if (count === 0) {
      played.current = view;
      return;
    }
    const element = viewport();
    const rows = element?.querySelectorAll<HTMLElement>(".row");
    // The virtualizer mounts its first rows a render after it measures; wait for them.
    if (!element || !rows?.length) return;
    played.current = view;
    if (reduced) return;
    const tokens = getComputedStyle(element);
    const duration = parseFloat(tokens.getPropertyValue("--t-med"));
    const easing = tokens.getPropertyValue("--ease-out").trim();
    running.current = [...rows].slice(0, STAGGERED).map((row, position) => row.animate(
      [{ opacity: 0, translate: "0 6px" }, { opacity: 1, translate: "0 0" }],
      { duration, delay: Math.min(position * STAGGER_MS, MAX_DELAY_MS), easing, fill: "backwards" },
    ));
  });
}
