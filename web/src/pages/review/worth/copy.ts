// The worth screen's own words (templates/worth_card.html.j2, decision_tabs.html.j2,
// decision_row.html.j2, decision_details.html.j2, worth_search.html.j2, rendering.py
// `render_decision_table`). Words the other screens share are in lib/review/copy.ts.

import { TOAST } from "@/lib/review/copy"
import type { WorthTab } from "@/types/review"

import type { Pile } from "./piles"

/** The card's two buttons, and a decided row's flip button. */
export const ANSWER: Readonly<Record<Pile, string>> = { yes: "Yes", no: "No" }

export const TAB_LABEL: Readonly<Record<WorthTab, string>> = { review: "Review", ...ANSWER }

/** What the toast says once a decision is saved. */
export const DECIDED: Readonly<Record<Pile, string>> = { yes: TOAST.added, no: TOAST.rejected }

/** The card's collapsed note box. */
export const WHY = "Why? Give feedback (optional)"

export const SEARCH = {
  placeholder: "Search people…",
  label: "Search people by name",
  empty: "No matches",
} as const

/** A decided row's reason heading: "Why yes" on the Yes pile. */
export function whyHeading(pile: Pile): string {
  return `Why ${pile}`
}

export const WHO_THEY_ARE = "Who they are"

/** The flip button's name: "Mark Jordan Bravo No". */
export function flipLabel(name: string, to: Pile): string {
  return `Mark ${name} ${ANSWER[to]}`
}

/** A decided pile's scrolling list, named for a screen reader: "Yes decisions". */
export function listLabel(pile: Pile): string {
  return `${ANSWER[pile]} decisions`
}

/** Under the rows while the next page of the pile is read. */
export const LOADING_ROWS = "Loading…"
