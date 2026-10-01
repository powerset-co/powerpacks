// Where the feedback popover sits in its card (reconcile_review.js `feedbackPopover`).

import { must } from "@/lib/must"

/** Between the person menu's foot and the popover. */
const GAP = 8
/** The least the popover keeps from the card's right edge. */
const EDGE = 8

/** Offsets from the card's top and right edges, in pixels. */
export interface PopoverPlace {
  top: number
  right: number
}

/** Under the person menu, right edges in line. The card is the popover's positioning host. */
export function popoverPlace(menu: HTMLElement): PopoverPlace {
  const card = must(menu.closest(".identity-card"), "the menu's card")
  const menuBox = menu.getBoundingClientRect()
  const cardBox = card.getBoundingClientRect()
  return {
    top: menuBox.bottom - cardBox.top + card.scrollTop + GAP,
    right: Math.max(EDGE, cardBox.right - menuBox.right),
  }
}
