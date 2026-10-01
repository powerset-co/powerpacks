import "../styles/worth.css"

import type { WorthTab } from "@/types/review"

import { PileTable } from "./PileTable"
import { usePileMoves } from "./usePileMoves"
import { WorthQueue } from "./WorthQueue"
import { WorthTabs } from "./WorthTabs"

export interface WorthStageProps {
  /** The tab the URL opened: the pending queue, or a decided pile. */
  tab: WorthTab
}

// The worth stage: the tabs with their counts, then the tab's content. Review is the card
// queue with its typeahead; Yes and No are the decided piles' tables. A decision counts on the
// tabs the moment it is clicked.
export function WorthStage({ tab }: WorthStageProps) {
  const moves = usePileMoves()
  return (
    <div className="worth-stage">
      <WorthTabs active={tab} counts={moves.counts} />
      {tab === "review" ? (
        <WorthQueue moves={moves} />
      ) : (
        <div className="worth-panel">
          <PileTable pile={tab} moves={moves} />
        </div>
      )}
    </div>
  )
}
