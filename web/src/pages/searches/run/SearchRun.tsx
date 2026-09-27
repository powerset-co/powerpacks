import { useMemo, useState, type ReactNode } from "react"

import type { PondCandidate, SearchRunPayload, Tagged } from "@/types/searches"

import { pondRows, rankResults, type Results } from "../lib/ranking"
import { PondChain } from "./PondChain"
import { ResultsTable, type ResultItem } from "./ResultsTable"
import { RunHeader } from "./RunHeader"
import { TeamPanel } from "./TeamPanel"

export interface SearchRunProps {
  payload: SearchRunPayload
  // The run's status from its catalog card.
  status?: string
  // Slots for the tags, feedback and export controls (web/README.md "Searches").
  toolbar?: ReactNode
  headerActions?: ReactNode
  rowActions?: (candidate: PondCandidate) => ReactNode
  tags?: Tagged | null
}

function rankedItems(results: Extract<Results, { mode: "ranked" }>): ResultItem[] {
  return results.sections.flatMap((section) => [
    ...(section.heading
      ? [{ kind: "heading" as const, key: `heading:${section.heading}`, text: section.heading }]
      : []),
    ...section.rows.map((result) => ({ kind: "row" as const, key: result.key, result })),
  ])
}

// One saved run: header, pond chain, the slots' toolbar, the people, the team.
export function SearchRun({ payload, status, toolbar, headerActions, rowActions, tags }: SearchRunProps) {
  const { search } = payload
  const results = useMemo(() => rankResults(search), [search])
  const [pondAt, setPond] = useState(0)
  const pond = search.ponds[pondAt]

  const items = useMemo((): ResultItem[] => {
    if (results.mode === "ranked") return rankedItems(results)
    if (results.mode === "unavailable" || !pond) return []
    return pondRows(search, pond).map((result) => ({ kind: "row", key: result.key, result }))
  }, [results, search, pond])

  const people = new Set(items.flatMap((item) => (item.kind === "row" ? [item.result.row.person_id] : [])))
    .size
  const empty =
    results.mode === "unavailable"
      ? "Screening scores are unavailable for this run."
      : pond
        ? `0 of ${pond.result_count.toLocaleString()} people this pond found scored 0.7 or higher, so none were kept.`
        : "This run has no ponds yet."

  return (
    <div className="search-run" data-search-run>
      <RunHeader search={search} status={status} people={people} actions={headerActions} />
      <PondChain
        ponds={search.ponds}
        selected={results.mode === "ponds" ? pondAt : null}
        onSelect={setPond}
      />
      {toolbar ? (
        <div className="run-toolbar" data-run-toolbar>
          {toolbar}
        </div>
      ) : null}
      <ResultsTable
        items={items}
        ranked={results.mode === "ranked"}
        empty={empty}
        tags={tags}
        rowActions={rowActions}
      />
      <TeamPanel search={search} />
    </div>
  )
}
