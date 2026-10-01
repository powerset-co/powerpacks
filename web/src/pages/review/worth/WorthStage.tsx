import type { WorthTab } from "@/types/review"

export interface WorthStageProps {
  /** The tab the URL opened: the pending queue, or a decided pile. */
  tab: WorthTab
}

// Stub: the worth stage (tabs, search, the card queue, the Yes / No tables) replaces this body.
export function WorthStage({ tab }: WorthStageProps) {
  return <div className="worth-stage" data-tab={tab} />
}
