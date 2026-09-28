import { memo, useEffect, useRef, useState } from "react"

import { useReducedMotion } from "@/hooks/useReducedMotion"

import { Fold } from "@/components/shared"
import { FACETS, type FacetDef, type FacetFilters, type FacetKey } from "@/lib/people/facets"
import { EMPTY, toggled } from "@/lib/sets"

import { RailFacet } from "./RailFacet"
import { ShortcutsHint } from "./ShortcutsHint"

const DEFAULT_FACETS = FACETS.filter((facet) => !facet.more)
const MORE_FACETS = FACETS.filter((facet) => facet.more)
const NO_COUNTS: ReadonlyMap<string, number> = new Map()

interface FacetRailProps {
  filters: FacetFilters
  counts: ReadonlyMap<FacetKey, ReadonlyMap<string, number>>
  onValue: (key: FacetKey, value: string) => void
}

// The default facets, "More filters" for the rest, and the keyboard shortcuts.
export const FacetRail = memo(function FacetRail({ filters, counts, onValue }: FacetRailProps) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(EMPTY)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(EMPTY)
  const [moreOpen, setMoreOpen] = useState(false)
  const [labelSearch, setLabelSearch] = useState("")
  const reduced = useReducedMotion()
  const moreToggle = useRef<HTMLButtonElement>(null)

  // The extra facets unfold below the visible rail: bring the toggle to the top so they show.
  useEffect(() => {
    if (moreOpen)
      moreToggle.current?.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" })
  }, [moreOpen, reduced])

  const facet = (def: FacetDef) => (
    <RailFacet
      key={def.key}
      facet={def}
      counts={counts.get(def.key) ?? NO_COUNTS}
      held={filters.get(def.key) ?? EMPTY}
      open={!collapsed.has(def.key)}
      expanded={expanded.has(def.key)}
      labelSearch={labelSearch}
      onToggleOpen={() => setCollapsed(toggled(collapsed, def.key))}
      onExpand={(on) => setExpanded(toggled(expanded, def.key, on))}
      onLabelSearch={setLabelSearch}
      onValue={(value) => onValue(def.key, value)}
    />
  )

  return (
    <>
      {DEFAULT_FACETS.map(facet)}
      <button
        ref={moreToggle}
        type="button"
        className="rail-divider chevron"
        data-more-toggle
        aria-expanded={moreOpen}
        onClick={() => setMoreOpen(!moreOpen)}
      >
        More filters
      </button>
      <Fold open={moreOpen} className="rail-more">
        {MORE_FACETS.map(facet)}
      </Fold>
      <ShortcutsHint />
    </>
  )
})
