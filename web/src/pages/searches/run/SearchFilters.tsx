import { ReadRow, Row, TraitChips } from "@/components/shared"
import { filledFields } from "@/lib/agent/searchFields"
import type { Pond } from "@/types/searches"

interface SearchFiltersProps {
  // The run's title: a quick search is titled by its query, a deep search by its role.
  title: string
  pond: Pond
}

// A one-pond run's search where the chain would sit: its query unless the title already is it,
// its traits and the filters it ran with, as the chat card showed them before Execute. A pond
// saved without them (a deep run) shows its query alone.
export function SearchFilters({ title, pond }: SearchFiltersProps) {
  const values = new Map(Object.entries(pond.filters))
  return (
    <section className="run-filters" aria-label="Search filters">
      {pond.query !== title && (
        <Row label="Query">
          <span className="text-[13px]">{pond.query}</span>
        </Row>
      )}
      {pond.traits.length > 0 && (
        <Row label="Traits">
          <TraitChips traits={pond.traits} />
        </Row>
      )}
      {filledFields(values, values).map((field) => (
        <ReadRow key={field.label} field={field} values={values} />
      ))}
    </section>
  )
}
