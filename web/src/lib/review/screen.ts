// What a loaded screen shows in place of its stage (server.py `full_page`).

import type { ReviewPage } from "@/types/review"

/** The Enrich panel is on screen, so a running job's numbers have somewhere to go. */
export function enrichPanelShown(page: ReviewPage): boolean {
  return page.view === "enrich" && !page.needs_synthesis
}
