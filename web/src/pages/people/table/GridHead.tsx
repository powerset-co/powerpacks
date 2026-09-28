import { useEffect, useRef } from "react"

import { SortHeader, type SortDirection } from "@/components/shared"
import type { Sort, SortKey } from "@/lib/people/facets"

import { COLUMNS } from "./columns"

interface GridHeadProps {
  sort: Sort
  matching: number
  allSelected: boolean
  someSelected: boolean
  onSort: (key: SortKey) => void
  onSelectAll: () => void
}

function direction(sort: Sort, key: SortKey): SortDirection {
  if (sort.key !== key) return "none"
  return sort.dir === 1 ? "ascending" : "descending"
}

export function GridHead({ sort, matching, allSelected, someSelected, onSort, onSelectAll }: GridHeadProps) {
  const selectAll = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = someSelected
  }, [someSelected])

  return (
    <div className="grid-head" role="row" data-grid-head>
      <div role="columnheader" className="c-check">
        <label className="check">
          <input
            ref={selectAll}
            type="checkbox"
            data-select-all
            aria-label={`Select all ${matching.toLocaleString()} matching people`}
            checked={allSelected}
            onChange={onSelectAll}
          />
        </label>
      </div>
      {COLUMNS.map(({ sort: key, cls, label, right }) => {
        const className = right ? `${cls} right` : cls
        if (!key)
          return (
            <span key={cls} role="columnheader" className={className}>
              {label}
            </span>
          )
        return (
          <SortHeader
            key={cls}
            label={label}
            sort={direction(sort, key)}
            onSort={() => onSort(key)}
            className={className}
          />
        )
      })}
    </div>
  )
}
