import { FacetShell, FacetValue, Fold, SearchField } from "@/components/shared"
import { facetText, orderedValues, type FacetDef } from "@/lib/people/facets"

// A long facet shows this many values, unless only one more would be hidden.
const VISIBLE_VALUES = 8

interface RailFacetProps {
  facet: FacetDef
  counts: ReadonlyMap<string, number>
  held: ReadonlySet<string>
  open: boolean
  expanded: boolean
  labelSearch: string
  onToggleOpen: () => void
  onExpand: (expanded: boolean) => void
  onLabelSearch: (text: string) => void
  onValue: (value: string) => void
}

// One facet: its values with live counts, the label search, and "N more…" / "Show fewer",
// which folds the rest open and shut.
export function RailFacet(props: RailFacetProps) {
  const { facet, counts, held, open, expanded, labelSearch } = props
  let values = orderedValues(facet, counts, held)
  if (facet.search && labelSearch) {
    const needle = labelSearch.toLowerCase()
    values = values.filter((value) => facetText(facet, value).toLowerCase().includes(needle))
  }
  // Keep the shell, search box included, while a label search matches nothing.
  if (!values.length && !(facet.search && labelSearch)) return null
  const long = values.length > VISIBLE_VALUES + 1
  const first = long ? values.slice(0, VISIBLE_VALUES) : values
  const rest = long ? values.slice(VISIBLE_VALUES) : []
  const value = (name: string) => (
    <FacetValue
      key={name}
      label={facetText(facet, name)}
      count={counts.get(name) ?? 0}
      pressed={held.has(name)}
      onToggle={() => props.onValue(name)}
      data-facet-key={facet.key}
      data-facet-value={name}
    />
  )
  return (
    <FacetShell
      facetKey={facet.key}
      label={facet.label}
      open={open}
      active={held.size > 0}
      onToggle={props.onToggleOpen}
    >
      {facet.search ? (
        <SearchField
          className="facet-search"
          value={labelSearch}
          placeholder="Find a label"
          aria-label="Find a label"
          onChange={(event) => props.onLabelSearch(event.target.value)}
        />
      ) : null}
      {first.map(value)}
      {long ? (
        <>
          <Fold open={expanded}>{rest.map(value)}</Fold>
          <button
            type="button"
            className="facet-more"
            aria-expanded={expanded}
            onClick={() => props.onExpand(!expanded)}
          >
            {expanded ? "Show fewer" : `${rest.length.toLocaleString()} more…`}
          </button>
        </>
      ) : null}
    </FacetShell>
  )
}
