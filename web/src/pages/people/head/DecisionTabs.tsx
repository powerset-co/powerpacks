import type { ReactNode } from "react"

import { CountRoll, TabInk } from "@/components/shared"
import { label } from "@/lib/people/copy"
import { ORDER, type Decision } from "@/types/people"

interface DecisionTabsProps {
  tab: Decision
  totals: Readonly<Record<Decision, number>>
  onTab: (tab: Decision) => void
  /** The share button, at the right edge beside the tabs. */
  action: ReactNode
}

// The three decision counts are the tabs: counts roll, the ink slides under the selected one.
export function DecisionTabs({ tab, totals, onTab, action }: DecisionTabsProps) {
  return (
    <section className="people-head" data-head>
      {ORDER.map((decision) => (
        <button
          key={decision}
          type="button"
          className={`stat stat-${decision}`}
          data-tab={decision}
          aria-pressed={tab === decision}
          onClick={() => onTab(decision)}
        >
          <b>
            <CountRoll value={totals[decision]} />
          </b>
          <span>{label("share", decision)}</span>
        </button>
      ))}
      <TabInk active={`[data-tab='${tab}']`} />
      {action}
    </section>
  )
}
