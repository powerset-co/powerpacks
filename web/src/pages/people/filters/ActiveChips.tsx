import { Appear, Chip } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { usePresenceList } from "@/hooks/usePresenceList"
import { facetOf, facetText, type FacetFilters, type FacetKey } from "@/lib/people/facets"

interface ActiveChipsProps {
  filters: FacetFilters
  onRemove: (key: FacetKey, value: string) => void
  onClear: () => void
}

interface Held {
  key: FacetKey
  value: string
}

const heldKey = ({ key, value }: Held) => `${key}:${value}`

// One removable chip per held facet value, then "Clear filters"; each rises in and drops out.
export function ActiveChips({ filters, onRemove, onClear }: ActiveChipsProps) {
  const held = [...filters].flatMap(([key, values]) => [...values].map((value) => ({ key, value })))
  const chips = usePresenceList(held, heldKey)
  return (
    <span className="chip-row bar-chips" data-chips>
      {chips.map(({ key, item, open, onTransitionEnd }) => {
        const facet = facetOf(item.key)
        return (
          <Chip
            key={key}
            className="chip rise"
            data-open={open}
            pressed
            label={facet.label}
            removable
            title="Remove"
            data-chip-key={item.key}
            onClick={() => onRemove(item.key, item.value)}
            onTransitionEnd={onTransitionEnd}
          >
            {facetText(facet, item.value)}
          </Chip>
        )
      })}
      <Appear show={held.length > 0}>
        <Button variant="ghost" data-clear-filters onClick={onClear}>
          Clear filters
        </Button>
      </Appear>
    </span>
  )
}
