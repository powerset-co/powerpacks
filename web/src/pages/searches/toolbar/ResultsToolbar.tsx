import { CountRoll, type ToastMessage } from "@/components/shared"
import { Chip } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { copyRows, csvFilename, downloadCsv } from "@/lib/searches/exports"
import {
  countText,
  exportScore,
  filterRows,
  liveFilters,
  operatorOptions,
  saveLabels,
  taggedCount,
  type ResultFilters,
  type ToolbarRow,
} from "@/lib/searches/filters"
import { heldTags } from "@/lib/searches/tags"
import { cn } from "@/lib/utils"
import type { Tagged } from "@/types/searches"

import { Appear } from "./Appear"
import { ClearTags } from "./ClearTags"
import { OperatorPicker } from "./OperatorPicker"
import { ResultCount } from "./ResultCount"
import { ScoreFilter } from "./ScoreFilter"
import { TagFilters } from "./TagFilters"

export interface ResultsToolbarProps {
  // The search title: the CSV filename's words.
  title: string
  // Every row of the panel, before filtering.
  rows: readonly ToolbarRow[]
  // The overall table has 1–5 scores to filter on; a pond's trait table does not.
  scored: boolean
  tagged: Tagged
  filters: ResultFilters
  onFiltersChange: (filters: ResultFilters) => void
  onUntag: (personIds: readonly string[]) => void
  onClearTags: () => void
  onAnnounce: (toast: ToastMessage) => void
  // Placement from the page, e.g. sticky under the top bar as results.css has it.
  className?: string
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

// rendering.py _results_toolbar: Tagged (n), Labels, Overall score, Operators, tag filter,
// then the count and Untag all on page / Copy / CSV / Clear all. Export and copy take every
// filtered row, never only the mounted ones.
export function ResultsToolbar({
  title,
  rows,
  scored,
  tagged,
  filters,
  onFiltersChange,
  onUntag,
  onClearTags,
  onAnnounce,
  className,
}: ResultsToolbarProps) {
  const live = liveFilters(filters, rows, tagged)
  const count = taggedCount(rows, tagged)
  const shown = filterRows(rows, filters, tagged)
  const exported = filterRows(rows, filters, tagged, exportScore)
  const total = new Set(rows.map((row) => row.row.person_id)).size
  const change = (patch: Partial<ResultFilters>) => onFiltersChange({ ...live, ...patch })

  const exportCsv = () => {
    const ids = exported.map((row) => row.row.person_id)
    downloadCsv(exported, csvFilename(title, heldTags(tagged, ids)))
    onAnnounce({ message: `Exported ${countText(exported.length, exported.length)}.` })
  }
  const copy = () => {
    copyRows(exported).then(
      () => onAnnounce({ message: `Copied ${countText(exported.length, exported.length)}.` }),
      (error: unknown) => onAnnounce({ message: `Couldn't copy. ${errorText(error)}`, error: true }),
    )
  }

  return (
    <div
      className={cn(
        "flex min-h-11 flex-wrap items-center gap-2.5 border-b border-line bg-[color-mix(in_srgb,var(--card)_92%,transparent)] py-2 backdrop-blur",
        className,
      )}
      role="toolbar"
      aria-label="Results"
      data-results-toolbar
    >
      <Appear show={count > 0}>
        <Chip pressed={live.taggedOnly} onClick={() => change({ taggedOnly: !live.taggedOnly })}>
          {/* One span: the chip spaces its children apart. */}
          <span>
            Tagged (<CountRoll value={count} />)
          </span>
        </Chip>
      </Appear>
      <Chip
        pressed={live.labels}
        title="Show or hide Taste, Suggested pin and Team similarity labels"
        onClick={() => {
          saveLabels(!live.labels)
          change({ labels: !live.labels })
        }}
      >
        Labels
      </Chip>
      {scored ? <ScoreFilter scores={live.scores} onChange={(scores) => change({ scores })} /> : null}
      <OperatorPicker
        operators={operatorOptions(rows)}
        selected={live.operators}
        onChange={(operators) => change({ operators })}
      />
      <Appear show={live.taggedOnly && tagged.tags.length > 0}>
        <TagFilters tags={tagged.tags} selected={live.tags} onChange={(tags) => change({ tags })} />
      </Appear>
      <span className="ml-auto inline-flex flex-wrap items-center gap-1.5">
        <ResultCount shown={shown.length} total={total} />
        <Appear show={live.taggedOnly && shown.length > 0}>
          <Button variant="ghost" size="sm" onClick={() => onUntag(shown.map((row) => row.row.person_id))}>
            Untag all on page
          </Button>
        </Appear>
        <Button size="sm" shape="pill" disabled={!exported.length} onClick={copy}>
          Copy
        </Button>
        <Button variant="primary" size="sm" shape="pill" disabled={!exported.length} onClick={exportCsv}>
          CSV
        </Button>
        <Appear show={live.taggedOnly && shown.length > 0}>
          <ClearTags onClear={onClearTags} />
        </Appear>
      </span>
    </div>
  )
}
