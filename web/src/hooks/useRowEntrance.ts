import { useLayoutEffect, useRef } from "react";

import type { SavedView } from "@/lib/storage";

import { useReducedMotion } from "./useReducedMotion";

const STAGGERED = 14;
const STAGGER_MS = 14;
const MAX_DELAY_MS = 100;

/**
 * Rows fade and rise in after the view changes (tab, filter, search, sort): the first
 * fourteen mounted rows, 14ms apart. Scrolling, selection and writes never replay it.
 * `view` changes identity exactly when the view does. Timing is index.css's --t-med and --ease-out.
 */
export function useRowEntrance(viewport: () => HTMLElement | null, view: SavedView, count: number) {
  const reduced = useReducedMotion();
  const played = useRef<SavedView | null>(null);

  useLayoutEffect(() => {
    if (played.current === view) return;
    if (count === 0) {
      played.current = view;
      return;
    }
    const element = viewport();
    const rows = element?.querySelectorAll<HTMLElement>(".row");
    // The virtualizer mounts its first rows a render after it measures; wait for them.
    if (!rows?.length) return;
    played.current = view;
    if (reduced) return;
    const tokens = getComputedStyle(element!);
    const duration = parseFloat(tokens.getPropertyValue("--t-med"));
    const easing = tokens.getPropertyValue("--ease-out").trim();
    [...rows].slice(0, STAGGERED).forEach((row, position) => {
      row.getAnimations().forEach((animation) => animation.cancel());
      row.animate(
        [{ opacity: 0, translate: "0 6px" }, { opacity: 1, translate: "0 0" }],
        { duration, delay: Math.min(position * STAGGER_MS, MAX_DELAY_MS), easing, fill: "backwards" },
      );
    });
  });
}
