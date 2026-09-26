import { useRef, useState, type TransitionEvent } from "react";

import type { DetailState } from "@/hooks/usePersonDetail";
import { useReducedMotion } from "@/hooks/useReducedMotion";
import type { Person } from "@/types/people";

interface Shown {
  id: string | null;
  open: boolean;
  swapping: boolean;
}

/**
 * What the drawer renders while it switches person (polish-final D): the outgoing person
 * stays, `leaving`, for a 60ms fade to zero; on its transitionend the new person replaces
 * them together and fades in (`swapping`). A first open slides the panel instead, and
 * reduced motion replaces at once.
 */
export function useDrawerSwap(row: Person | null, detail: DetailState, open: boolean) {
  const reduced = useReducedMotion();
  const id = row?.parent_id ?? null;
  const [shown, setShown] = useState<Shown>({ id, open, swapping: false });
  const outgoing = useRef({ row, detail });

  const leaving = !reduced && shown.open && open && shown.id !== null && id !== null && shown.id !== id;
  if (!leaving && (shown.id !== id || shown.open !== open)) {
    setShown({ id, open, swapping: shown.id !== id ? shown.open && open : shown.swapping });
  }
  if (!leaving) outgoing.current = { row, detail };

  const onTransitionEnd = (event: TransitionEvent<HTMLElement>) => {
    if (leaving && event.target === event.currentTarget) setShown({ id, open, swapping: true });
  };

  return { ...outgoing.current, leaving, swapping: shown.swapping, onTransitionEnd };
}
