import { memo } from "react"

import { Chip } from "@/components/shared"
import { QUICK, quickActive, type FacetFilters, type QuickFilter } from "@/lib/people/facets"

interface QuickFiltersProps {
  filters: FacetFilters
  counts: readonly number[]
  onPick: (quick: QuickFilter | null) => void
}

// Named facet selections counted within the tab; pressing the active one clears it.
export const QuickFilters = memo(function QuickFilters({ filters, counts, onPick }: QuickFiltersProps) {
  const available = QUICK.map((quick, position) => ({ quick, position, count: counts[position] ?? 0 }))
    .filter(({ count }) => count > 0)
    .sort((a, b) => Number(!!a.quick.set.logbook) - Number(!!b.quick.set.logbook) || b.count - a.count)
  return (
    <section className="quick" data-quick aria-label="Quick filters">
      {available.map(({ quick, position, count }) => {
        const active = quickActive(quick, filters)
        return (
          <Chip
            key={quick.name}
            className="chip"
            pressed={active}
            count={count}
            data-quick-index={position}
            onClick={() => onPick(active ? null : quick)}
          >
            {quick.name}
          </Chip>
        )
      })}
    </section>
  )
})
