import { useState, type TransitionEvent } from "react"

import { useReducedMotion } from "@/hooks/useReducedMotion"
import type { Person } from "@/types/people"

import type { DetailState } from "../hooks/usePersonDetail"

interface Shown {
  id: string | null
  open: boolean
}

interface Content {
  row: Person | null
  detail: DetailState
}

/**
 * What the drawer renders while it switches person. Two readable people never show at
 * once: the outgoing person stays, `leaving`, while their content fades to zero (--t-fast);
 * when that opacity transition ends the new person replaces them and fades in from
 * @starting-style (overlays.css .drawer-inner). A click on yet another person while one
 * is leaving just changes who comes next. Reduced motion replaces at once.
 */
export function useDrawerSwap(row: Person | null, detail: DetailState, open: boolean) {
  const reduced = useReducedMotion()
  const id = row?.parent_id ?? null
  const [shown, setShown] = useState<Shown>({ id, open })
  // The last content rendered while not leaving: what stays on screen during the fade out.
  const [last, setLast] = useState<Content>({ row, detail })

  const leaving = !reduced && shown.open && open && shown.id !== null && id !== null && shown.id !== id
  if (!leaving && (shown.id !== id || shown.open !== open)) setShown({ id, open })
  if (!leaving && (last.row !== row || last.detail !== detail)) setLast({ row, detail })

  const onTransitionEnd = (event: TransitionEvent<HTMLElement>) => {
    if (leaving && event.target === event.currentTarget && event.propertyName === "opacity") {
      setShown({ id, open })
    }
  }

  const content = leaving ? last : { row, detail }
  return { ...content, leaving, onTransitionEnd }
}
