import { Chip } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { usePresence } from "@/hooks/usePresence"
import { usePresenceList } from "@/hooks/usePresenceList"
import { FACET_BY_KEY, facetText } from "@/lib/people/facets"

interface ActiveChipsProps {
  filters: ReadonlyMap<string, ReadonlySet<string>>
  onRemove: (key: string, value: string) => void
  onClear: () => void
}

interface Held {
  key: string
  value: string
}

const heldKey = ({ key, value }: Held) => `${key}:${value}`

// One removable chip per held facet value, then "Clear filters"; each rises in and drops out.
export function ActiveChips({ filters, onRemove, onClear }: ActiveChipsProps) {
  const held = [...filters].flatMap(([key, values]) => [...values].map((value) => ({ key, value })))
  const chips = usePresenceList(held, heldKey)
  const clear = usePresence(held.length > 0 ? held.length : null)
  return (
    <span className="chip-row bar-chips" data-chips>
      {chips.map(({ key, item, open, onTransitionEnd }) => {
        const facet = FACET_BY_KEY.get(item.key)
        if (!facet) return null
        return (
          <Chip
            key={key}
            className="chip rise"
            data-open={open}
            pressed
            title="Remove"
            data-chip-key={item.key}
            onClick={() => onRemove(item.key, item.value)}
            onTransitionEnd={onTransitionEnd}
          >
            <em>{facet.label}</em> {facetText(facet, item.value)}
            <span className="x" aria-hidden="true">
              ×
            </span>
          </Chip>
        )
      })}
      {clear.mounted ? (
        <Button
          variant="ghost"
          className="rise bar-clear"
          data-open={clear.open}
          data-clear-filters
          onClick={onClear}
          onTransitionEnd={clear.onTransitionEnd}
        >
          Clear filters
        </Button>
      ) : null}
    </span>
  )
}
