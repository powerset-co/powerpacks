import { Chip } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { FACET_BY_KEY, facetText } from "@/lib/people/facets";

interface ActiveChipsProps {
  filters: ReadonlyMap<string, ReadonlySet<string>>;
  onRemove: (key: string, value: string) => void;
  onClear: () => void;
}

// One removable chip per held facet value, then "Clear filters".
export function ActiveChips({ filters, onRemove, onClear }: ActiveChipsProps) {
  const chips = [...filters].flatMap(([key, values]) => [...values].map((value) => ({ key, value })));
  return (
    <span className="chip-row bar-chips" data-chips>
      {chips.map(({ key, value }) => {
        const facet = FACET_BY_KEY.get(key);
        if (!facet) return null;
        return (
          <Chip key={`${key}:${value}`} className="chip" pressed title="Remove" data-chip-key={key} onClick={() => onRemove(key, value)}>
            <em>{facet.label}</em> {facetText(facet, value)}
            <span className="x" aria-hidden="true">×</span>
          </Chip>
        );
      })}
      {chips.length ? (
        <Button variant="ghost" className="bar-clear" data-clear-filters onClick={onClear}>Clear filters</Button>
      ) : null}
    </span>
  );
}
