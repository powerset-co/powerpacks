import { useQueryClient } from "@tanstack/react-query"
import { useMemo, useState } from "react"

import type { ToastMessage } from "@/components/shared"
import { exportScoreOf, queuedScores, withScore, yourScore } from "@/lib/searches/feedback"
import { filterRows, keptRows, labelsShown, NO_FILTERS, type ResultFilters } from "@/lib/searches/filters"
import { panelSections, rankResults, type ResultRow } from "@/lib/searches/ranking"
import type { FeedbackRecord, SearchRunPayload } from "@/types/searches"

import { SearchFeedbackDialog } from "../dialogs/SearchFeedbackDialog"
import type { Feedback } from "../hooks/useFeedback"
import { searchRunKey } from "../hooks/useSearchRun"
import { useSearchTags } from "../hooks/useSearchTags"
import { ResultsToolbar } from "../toolbar/ResultsToolbar"
import { RowActions } from "./RowActions"
import { SearchRun } from "./SearchRun"

interface RunViewProps {
  payload: SearchRunPayload
  status: string | undefined
  feedback: Feedback
  onToast: (toast: ToastMessage) => void
}

/**
 * One open run with its controls: the tags (loaded per run), the toolbar's filters, the
 * people they keep, and each person's own score. The pane is keyed to the run, so filters
 * start fresh on every run. The table and the count read the same filtered rows.
 */
export function RunView({ payload, status, feedback, onToast }: RunViewProps) {
  const { search, ratings } = payload
  const runId = search.run_id
  const queryClient = useQueryClient()
  const tags = useSearchTags(runId, onToast)
  const [filters, setFilters] = useState<ResultFilters>(() => ({ ...NO_FILTERS, labels: labelsShown() }))
  const [pondAt, setPond] = useState(0)

  const results = useMemo(() => rankResults(search), [search])
  const sections = useMemo(() => panelSections(search, results, pondAt), [search, results, pondAt])
  const rows = useMemo(() => sections.flatMap((section) => section.rows), [sections])
  const kept = useMemo(() => new Set(keptRows(rows, filters, tags.tagged)), [rows, filters, tags.tagged])
  const shownSections = useMemo(
    () =>
      sections
        .map((section) => ({ ...section, rows: section.rows.filter((row) => kept.has(row)) }))
        .filter((section) => section.rows.length),
    [sections, kept],
  )
  const shown = useMemo(() => filterRows(rows, filters, tags.tagged), [rows, filters, tags.tagged])
  const people = new Set(rows.map((row) => row.row.person_id)).size

  const queued = useMemo(
    () => queuedScores(feedback.pending, runId, ratings.legacy),
    [feedback.pending, runId, ratings.legacy],
  )
  // The row shows a saved score at once; the server returns it on the next read.
  const saved = (record: FeedbackRecord) => {
    queryClient.setQueryData<SearchRunPayload>(searchRunKey(runId), (current) =>
      current ? withScore(current, record) : current,
    )
  }

  const rowActions = (result: ResultRow) =>
    result.candidate ? (
      <RowActions
        runId={runId}
        candidate={result.candidate}
        rubric={ratings.rubric}
        tags={tags}
        score={yourScore(result, queued)}
        submit={feedback.submit}
        onSaved={saved}
        onToast={onToast}
      />
    ) : null

  return (
    <SearchRun
      search={search}
      status={status}
      mode={results.mode}
      pondAt={pondAt}
      onPond={setPond}
      people={people}
      sections={shownSections}
      filtered={shown.length < people}
      labels={filters.labels}
      toolbar={
        <ResultsToolbar
          title={search.title}
          rows={rows}
          shown={shown}
          scored={results.mode === "ranked"}
          tagged={tags.tagged}
          filters={filters}
          exportScore={exportScoreOf(queued)}
          onFiltersChange={setFilters}
          onUntag={tags.untag}
          onClearTags={tags.clear}
          onAnnounce={onToast}
        />
      }
      headerActions={
        <SearchFeedbackDialog runId={runId} title={search.title} submit={feedback.submit} onToast={onToast} />
      }
      rowActions={rowActions}
    />
  )
}
