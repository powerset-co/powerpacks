import { forwardRef } from "react"

import { Chip, SearchField } from "@/components/shared"

import type { CatalogFilter } from "../lib/catalog"

export const SEARCH_PLACEHOLDER = "Search title, company, run"

interface CatalogFiltersProps {
  filter: CatalogFilter
  versions: readonly string[]
  companies: readonly string[]
  statuses: readonly string[]
  disabled?: boolean
  onChange: (next: CatalogFilter) => void
}

const SELECT =
  "h-7 min-w-0 flex-1 rounded-sm border border-border bg-card px-1.5 text-[11.5px] text-foreground outline-none transition-[border-color] duration-fast ease-out focus:border-primary disabled:opacity-45"

// The search box, then company and status, then the version chips (newest first, "All" last).
export const CatalogFilters = forwardRef<HTMLInputElement, CatalogFiltersProps>(
  ({ filter, versions, companies, statuses, disabled = false, onChange }, ref) => (
    <div className="catalog-filters" data-catalog-filters>
      <SearchField
        ref={ref}
        className="w-full"
        data-filter-text
        placeholder={SEARCH_PLACEHOLDER}
        aria-label="Search saved searches"
        value={filter.text}
        disabled={disabled}
        onChange={(event) => onChange({ ...filter, text: event.target.value })}
      />
      <div className="flex gap-1.5">
        <select
          className={SELECT}
          data-filter="company"
          aria-label="Company"
          value={filter.company}
          disabled={disabled}
          onChange={(event) => onChange({ ...filter, company: event.target.value })}
        >
          <option value="">All companies</option>
          {companies.map((company) => (
            <option key={company} value={company}>
              {company}
            </option>
          ))}
        </select>
        <select
          className={SELECT}
          data-filter="status"
          aria-label="Status"
          value={filter.status}
          disabled={disabled}
          onChange={(event) => onChange({ ...filter, status: event.target.value })}
        >
          <option value="">All statuses</option>
          {statuses.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
      </div>
      {versions.length ? (
        <div className="flex flex-wrap gap-1" role="group" aria-label="Search version">
          {versions.map((version) => (
            <Chip
              key={version}
              className="min-h-6 px-2 text-[11px]"
              data-version={version}
              pressed={filter.version === version}
              onClick={() => onChange({ ...filter, version })}
            >
              {version}
            </Chip>
          ))}
          <Chip
            className="min-h-6 px-2 text-[11px]"
            data-version="all"
            pressed={filter.version === null}
            onClick={() => onChange({ ...filter, version: null })}
          >
            All
          </Chip>
        </div>
      ) : null}
    </div>
  ),
)
CatalogFilters.displayName = "CatalogFilters"
