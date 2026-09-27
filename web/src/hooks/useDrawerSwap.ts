import { useState, type TransitionEvent } from "react"

import { ended } from "./usePresence"
import { useReducedMotion } from "./useReducedMotion"

interface Shown {
  id: string | null
  open: boolean
}

/**
 * What a drawer renders while it switches from one item to another (`id` names the item,
 * `content` is what it draws; keep `content`'s identity stable while it is unchanged). Two
 * readable items never show at once: the outgoing content stays, `leaving`, while it fades to
 * zero (--t-fast); when that opacity transition ends the new content replaces it and fades in
 * from @starting-style (components/shared/Drawer.css .drawer-inner). A switch to yet another
 * item while one is leaving just changes what comes next. Reduced motion replaces at once.
 */
export function useDrawerSwap<T>(id: string | null, content: T, open: boolean) {
  const reduced = useReducedMotion()
  const [shown, setShown] = useState<Shown>({ id, open })
  // The last content rendered while not leaving: what stays on screen during the fade out.
  const [last, setLast] = useState<T>(content)

  const leaving = !reduced && shown.open && open && shown.id !== null && id !== null && shown.id !== id
  if (!leaving && (shown.id !== id || shown.open !== open)) setShown({ id, open })
  if (!leaving && last !== content) setLast(content)

  const onTransitionEnd = (event: TransitionEvent<HTMLElement>) => {
    if (leaving && ended(event)) setShown({ id, open })
  }

  // `shownId` keys the drawer's content: the outgoing item's while it leaves.
  return { shownId: leaving ? shown.id : id, content: leaving ? last : content, leaving, onTransitionEnd }
}
