import { useMemo, type ReactNode } from "react"

import { isComplete } from "@/lib/searches/copy"
import type { ResultSection, Results } from "@/lib/searches/ranking"
import type { SearchResult } from "@/types/searches"

import { PondChain } from "./PondChain"
import { ResultsTable, type ResultItem } from "./ResultsTable"
import type { RowContext } from "./RowActions"
import { RunHeader } from "./RunHeader"
import { RowsSkeleton } from "./RunLoading"
import { SearchFilters } from "./SearchFilters"

export interface SearchRunProps {
  search: SearchResult
  // The run's status from its catalog card; none until the catalog loads.
  status: string | undefined
  mode: Results["mode"]
  // Without a screen the pond chain picks the table's pond.
  pondAt: number
  onPond: (index: number) => void
  // With a screen: the pond the list is narrowed to, or null for every pond.
  pondOnly: number | null
  onPondOnly: (index: number) => void
  // The panel's people before filtering, and the sections the filters keep.
  people: number
  sections: readonly ResultSection[]
  filtered: boolean
  // Taste, Suggested pin and Team similarity labels on each row.
  labels: boolean
  // The run's controls (web/README.md "Searches").
  toolbar: ReactNode
  headerActions: ReactNode
  rowContext: RowContext
}

function tableItems(sections: readonly ResultSection[]): ResultItem[] {
  return sections.flatMap((section) => [
    ...(section.heading
      ? [{ kind: "heading" as const, key: `heading:${section.heading}`, text: section.heading }]
      : []),
    ...section.rows.map((result) => ({ kind: "row" as const, key: result.key, result })),
  ])
}

function emptyText(search: SearchResult, mode: Results["mode"], pondAt: number, filtered: boolean): string {
  if (filtered) return "No one here matches these filters."
  if (mode === "unavailable") return "Screening scores are unavailable for this run."
  const pond = search.ponds[pondAt]
  return pond
    ? `0 of ${pond.result_count.toLocaleString()} people this pond found scored 0.7 or higher, so none were kept.`
    : "This run has no ponds yet."
}

// One saved run: header (with the job description and the team), the pond chain (or, with one
// pond, the search it ran), the toolbar, the people the filters keep.
export function SearchRun(props: SearchRunProps) {
  const { search, mode, pondAt, sections, filtered, status } = props
  // One identity per filtered list: the table's row keys and measurements follow it.
  const items = useMemo(() => tableItems(sections), [sections])
  // A plain run's rows arrive screened and the ranking reorders them, so the table waits for it.
  // A deep run shows its finished ponds while the next one runs; until one has people, it waits too.
  const running = status !== undefined && status !== "failed" && !isComplete(status)
  const ranking = running && !search.jd_text
  const waiting = ranking || (running && items.length === 0 && !filtered)
  return (
    <div className="search-run" data-search-run>
      <RunHeader search={search} status={props.status} people={props.people} actions={props.headerActions} />
      {search.ponds.length > 1 ? (
        <PondChain
          ponds={search.ponds}
          selected={mode === "ponds" ? pondAt : props.pondOnly}
          onSelect={mode === "ponds" ? props.onPond : props.onPondOnly}
        />
      ) : (
        search.ponds.map((pond) => <SearchFilters key={pond.pond_n} title={search.title} pond={pond} />)
      )}
      <div className="run-toolbar" data-run-toolbar>
        {props.toolbar}
      </div>
      {waiting ? (
        <div className="grid content-start" aria-busy="true" data-results-waiting>
          <p className="m-0 border-b border-line px-3 py-2 text-xs text-muted-foreground">
            {ranking ? `${props.people.toLocaleString()} screened, ranking…` : "Searching…"}
          </p>
          <RowsSkeleton />
        </div>
      ) : (
        <ResultsTable
          items={items}
          ranked={mode === "ranked"}
          labels={props.labels}
          empty={emptyText(search, mode, pondAt, filtered)}
          rowContext={props.rowContext}
        />
      )}
    </div>
  )
}
