import { forwardRef } from "react"

import { SearchField } from "@/components/shared"
import type { CatalogFilter } from "@/lib/searches/catalog"

export const SEARCH_PLACEHOLDER = "Search title, company, run"

interface CatalogSearchProps {
  filter: CatalogFilter
  disabled?: boolean
  onChange: (next: CatalogFilter) => void
}

// The one filter over the saved searches: a search box.
export const CatalogSearch = forwardRef<HTMLInputElement, CatalogSearchProps>(
  ({ filter, disabled = false, onChange }, ref) => (
    <SearchField
      ref={ref}
      className="w-full"
      data-filter-text
      placeholder={SEARCH_PLACEHOLDER}
      aria-label="Search saved searches"
      value={filter.text}
      disabled={disabled}
      onChange={(event) => onChange({ text: event.target.value })}
    />
  ),
)
CatalogSearch.displayName = "CatalogSearch"
