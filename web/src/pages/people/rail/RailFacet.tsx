import { FacetShell, FacetValue, SearchField } from "@/components/shared";
import { facetText, type FacetDef } from "@/lib/people/facets";

// A long facet shows this many values, unless only one more would be hidden.
const VISIBLE_VALUES = 8;

interface RailFacetProps {
  facet: FacetDef;
  counts: ReadonlyMap<string, number>;
  held: ReadonlySet<string>;
  open: boolean;
  expanded: boolean;
  labelSearch: string;
  onToggleOpen: () => void;
  onExpand: (expanded: boolean) => void;
  onLabelSearch: (text: string) => void;
  onValue: (value: string) => void;
}

function orderedValues(facet: FacetDef, counts: ReadonlyMap<string, number>, held: ReadonlySet<string>): string[] {
  const values = [...new Set([...counts.keys(), ...held])];
  const { order } = facet;
  if (order) return values.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  return values.sort((a, b) => (counts.get(b) ?? 0) - (counts.get(a) ?? 0) || a.localeCompare(b));
}

// One facet: its values with live counts, the label search, and "N more…" / "Show fewer".
export function RailFacet(props: RailFacetProps) {
  const { facet, counts, held, open, expanded, labelSearch } = props;
  let values = orderedValues(facet, counts, held);
  if (facet.search && labelSearch) {
    values = values.filter((value) => facetText(facet, value).toLowerCase().includes(labelSearch));
  }
  // Keep the shell, search box included, while a label search matches nothing.
  if (!values.length && !(facet.search && labelSearch)) return null;
  const long = values.length > VISIBLE_VALUES + 1;
  const shown = expanded || !long ? values : values.slice(0, VISIBLE_VALUES);
  const hidden = values.length - shown.length;
  return (
    <FacetShell facetKey={facet.key} label={facet.label} open={open} active={held.size > 0} onToggle={props.onToggleOpen}>
      {facet.search ? (
        <SearchField
          className="facet-search"
          value={labelSearch}
          placeholder="Find a label"
          aria-label="Find a label"
          onChange={(event) => props.onLabelSearch(event.target.value.toLowerCase())}
        />
      ) : null}
      {shown.map((value) => (
        <FacetValue
          key={value}
          label={facetText(facet, value)}
          count={counts.get(value) ?? 0}
          pressed={held.has(value)}
          onToggle={() => props.onValue(value)}
          data-facet-key={facet.key}
          data-facet-value={value}
        />
      ))}
      {hidden > 0 ? (
        <button type="button" className="facet-more" onClick={() => props.onExpand(true)}>{hidden} more…</button>
      ) : null}
      {expanded && long ? (
        <button type="button" className="facet-more" onClick={() => props.onExpand(false)}>Show fewer</button>
      ) : null}
    </FacetShell>
  );
}
