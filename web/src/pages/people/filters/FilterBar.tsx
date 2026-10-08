import { forwardRef, memo, type ReactNode } from "react"

import { SearchField } from "@/components/shared"
import { countOf } from "@/lib/copy"
import { SEARCH_LABEL, SEARCH_PLACEHOLDER } from "@/lib/people/copy"
import type { FacetFilters, FacetKey } from "@/lib/people/facets"

import { ActiveChips } from "./ActiveChips"

interface FilterBarProps {
  text: string
  filters: FacetFilters
  shown: number
  inTab: number
  onText: (text: string) => void
  onRemove: (key: FacetKey, value: string) => void
  onClear: () => void
  // A line before the count while something the filters read is unavailable.
  notice: ReactNode
}

// Search box, the held facet chips, and how many people are showing.
export const FilterBar = memo(
  forwardRef<HTMLInputElement, FilterBarProps>(
    ({ text, filters, shown, inTab, onText, onRemove, onClear, notice }, searchRef) => (
      <section className="bar" data-bar>
        <SearchField
          ref={searchRef}
          className="bar-search"
          data-search
          value={text}
          placeholder={SEARCH_PLACEHOLDER}
          aria-label={SEARCH_LABEL}
          onChange={(event) => onText(event.target.value)}
        />
        <ActiveChips filters={filters} onRemove={onRemove} onClear={onClear} />
        {notice}
        <span className="bar-count num" data-count aria-live="polite">
          {countOf(shown, inTab, "person")}
        </span>
      </section>
    ),
  ),
)
FilterBar.displayName = "FilterBar"
