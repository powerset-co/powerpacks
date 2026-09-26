import { useState } from "react";

import { FACETS, type FacetDef } from "@/lib/people/facets";
import { EMPTY, toggled } from "@/lib/sets";

import { RailFacet } from "./RailFacet";
import { ShortcutsHint } from "./ShortcutsHint";

const DEFAULT_FACETS = FACETS.filter((facet) => !facet.more);
const MORE_FACETS = FACETS.filter((facet) => facet.more);
const NO_COUNTS: ReadonlyMap<string, number> = new Map();

interface FacetRailProps {
  filters: ReadonlyMap<string, ReadonlySet<string>>;
  counts: ReadonlyMap<string, ReadonlyMap<string, number>>;
  onValue: (key: string, value: string) => void;
}

// The default facets, "More filters" for the rest, and the keyboard shortcuts.
export function FacetRail({ filters, counts, onValue }: FacetRailProps) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(EMPTY);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(EMPTY);
  const [moreOpen, setMoreOpen] = useState(false);
  const [labelSearch, setLabelSearch] = useState("");

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
  );

  return (
    <>
      {DEFAULT_FACETS.map(facet)}
      <button type="button" className="rail-divider" data-more-toggle aria-expanded={moreOpen} onClick={() => setMoreOpen(!moreOpen)}>
        More filters
      </button>
      {moreOpen ? MORE_FACETS.map(facet) : null}
      <ShortcutsHint />
    </>
  );
}
