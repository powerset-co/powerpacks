// The sidebar's folded state: its own toggle and, when a search's results open, AgentPage.

import { writeStored } from "@/lib/storage"

export const COLLAPSED_KEY = "sidebar.collapsed"
export const COLLAPSE_EVENT = "sidebar:collapse"

/** Collapse the sidebar to its rail so a search's results take the room. */
export function collapseSidebar(): void {
  writeStored("local", COLLAPSED_KEY, true)
  window.dispatchEvent(new Event(COLLAPSE_EVENT))
}
