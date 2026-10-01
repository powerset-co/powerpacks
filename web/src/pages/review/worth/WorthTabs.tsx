import { Link } from "react-router-dom"

import { tabHref } from "@/lib/review/links"
import { cn } from "@/lib/utils"
import type { WorthTab } from "@/types/review"

import { useReview } from "../hooks/useReview"
import { TAB_LABEL } from "./copy"
import type { TabCounts } from "./piles"

const TABS: readonly WorthTab[] = ["review", "yes", "no"]

interface WorthTabsProps {
  /** The tab on screen. */
  active: WorthTab
  counts: TabCounts
}

// templates/decision_tabs.html.j2: Review / Yes / No with their counts. A tab opens its
// screen; a deliberately opened screen (`preview=1`) stays one.
export function WorthTabs({ active, counts }: WorthTabsProps) {
  const { preview } = useReview()
  return (
    <nav className="decision-tabs">
      {TABS.map((tab) => (
        <Link
          key={tab}
          className={cn("decision-tab", tab === active && "active")}
          data-tab={tab}
          to={tabHref(tab, preview)}
        >
          {TAB_LABEL[tab]}
          <span>{counts[tab]}</span>
        </Link>
      ))}
    </nav>
  )
}
