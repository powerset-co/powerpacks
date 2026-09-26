import { useState, type TransitionEvent } from "react";

import { useReducedMotion } from "./useReducedMotion";

/**
 * Keeps an overlay mounted through its exit transition. Render while `mounted`, set
 * `data-open={open}` and pass `onTransitionEnd`; the `.rise` rule in index.css animates
 * both ways. Under reduced motion no transition runs, so it unmounts at once.
 */
export function usePresence(show: boolean) {
  const reduced = useReducedMotion();
  const [mounted, setMounted] = useState(show);
  if (show && !mounted) setMounted(true);
  if (!show && mounted && reduced) setMounted(false);

  const onTransitionEnd = (event: TransitionEvent<HTMLElement>) => {
    if (!show && event.target === event.currentTarget) setMounted(false);
  };
  return { mounted, open: show, onTransitionEnd };
}
