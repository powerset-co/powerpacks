import { CountRoll, TabInk } from "@/components/shared";
import { label, plural } from "@/lib/people/copy";
import { ORDER, type Decision } from "@/types/people";

interface DecisionTabsProps {
  tab: Decision;
  totals: Readonly<Record<Decision, number>>;
  total: number;
  onTab: (tab: Decision) => void;
}

// The three decision counts are the tabs: counts roll, the ink slides under the selected one.
export function DecisionTabs({ tab, totals, total, onTab }: DecisionTabsProps) {
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
          <b><CountRoll value={totals[decision]} /></b>
          <span>{label("share", decision)}</span>
        </button>
      ))}
      <TabInk active={`[data-tab='${tab}']`} />
      <span className="head-note">{plural(total, "person")}</span>
    </section>
  );
}
