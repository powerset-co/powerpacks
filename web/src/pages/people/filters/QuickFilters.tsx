import { Chip } from "@/components/shared";
import { QUICK, type QuickFilter } from "@/lib/people/facets";

import { quickActive } from "./quickActive";

interface QuickFiltersProps {
  filters: ReadonlyMap<string, ReadonlySet<string>>;
  counts: readonly number[];
  onPick: (quick: QuickFilter | null) => void;
}

// Named facet selections counted within the tab; pressing the active one clears it.
export function QuickFilters({ filters, counts, onPick }: QuickFiltersProps) {
  return (
    <section className="quick" data-quick aria-label="Quick filters">
      {QUICK.map((quick, position) => {
        const active = quickActive(quick, filters);
        const count = counts[position] ?? 0;
        return (
          <Chip
            key={quick.name}
            className="chip"
            pressed={active}
            count={count}
            disabled={!count && !active}
            data-quick-index={position}
            onClick={() => onPick(active ? null : quick)}
          >
            {quick.name}
          </Chip>
        );
      })}
    </section>
  );
}
