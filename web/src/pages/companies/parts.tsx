import type { ReactNode } from "react"

import { VirtualRows } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** Previous, "Page n of m", Next under a table; nothing when there is one page. */
export function Pager({
  page,
  pages,
  hasNext,
  disabled,
  onPage,
}: {
  page: number
  pages: number
  hasNext: boolean
  disabled: boolean
  onPage: (page: number) => void
}) {
  if (pages <= 1) return null
  return (
    <div className="flex items-center justify-center gap-4 border-t border-line px-5 py-3">
      <Button size="sm" variant="outline" disabled={disabled || page === 0} onClick={() => onPage(page - 1)}>
        Previous
      </Button>
      <span className="text-xs text-muted-foreground">
        Page {page + 1} of {pages}
      </span>
      <Button size="sm" variant="outline" disabled={disabled || !hasNext} onClick={() => onPage(page + 1)}>
        Next
      </Button>
    </div>
  )
}

/** A card's title line: the heading, and what sits at its right (a count, a search box). */
export function CardHead({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-4">
      <h2 className="m-0 text-base font-semibold">{title}</h2>
      {children}
    </div>
  )
}

export interface Column {
  label: string
  /** The header's and cells' class: alignment, or hidden when narrow. */
  className?: string
}

const ROW_ESTIMATE = 44

/** A page's table, as the People and Searches pages draw theirs: a header row, then the rows on
 *  VirtualRows, each row ruled above. `grid` is the grid template for the header and every row. A page holds at most
 *  100 rows, so all are mounted (overscan covers the page) and they scroll with the page. */
export function PageTable<T>({
  label,
  columns,
  grid,
  items,
  getKey,
  cells,
  rowClassName,
}: {
  label: string
  columns: readonly Column[]
  grid: string
  items: readonly T[]
  getKey: (item: T) => string
  cells: (item: T) => readonly ReactNode[]
  rowClassName?: string
}) {
  return (
    <div role="table" aria-label={label} aria-rowcount={items.length + 1}>
      <div
        role="row"
        className={cn(
          "grid h-8 items-center gap-2.5 px-5 text-[11px] max-[680px]:gap-2 font-extrabold uppercase tracking-[.08em] text-muted-foreground max-[680px]:px-4",
          grid,
        )}
      >
        {columns.map((column) => (
          <span key={column.label} role="columnheader" className={column.className}>
            {column.label}
          </span>
        ))}
      </div>
      <VirtualRows
        items={items}
        rowHeight={ROW_ESTIMATE}
        measure
        overscan={items.length}
        getKey={getKey}
        renderRow={(item) => (
          <div
            role="row"
            className={cn(
              "grid items-center gap-2.5 border-t border-line px-5 py-2.5 text-[13px] max-[680px]:gap-2 max-[680px]:px-4",
              grid,
              rowClassName,
            )}
          >
            {cells(item).map((cell, index) => (
              <div
                key={columns[index]?.label ?? index}
                role="cell"
                className={cn("min-w-0", columns[index]?.className)}
              >
                {cell}
              </div>
            ))}
          </div>
        )}
      />
    </div>
  )
}
