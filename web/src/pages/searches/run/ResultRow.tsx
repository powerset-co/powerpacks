import { memo, type MouseEvent } from "react"

import { Avatar } from "@/components/shared"
import type { ResultRow as Result } from "@/lib/searches/ranking"
import type { PondCandidate } from "@/types/searches"

import { TraitList } from "./Evidence"
import { JudgeBadges } from "./JudgeBadges"
import { LinkedInLink } from "./LinkedInLink"
import { NetworkSources } from "./NetworkSources"
import { RowScore, RowTags, type RowContext } from "./RowActions"
import { ScoreCell } from "./ScoreCell"

interface ResultRowProps {
  result: Result
  ranked: boolean
  labels: boolean
  // The drawer shows this row.
  open: boolean
  focused: boolean
  context: RowContext
  onToggle: (key: string) => void
}

function company(row: PondCandidate): string {
  return [row.company || "Company unknown", row.location].filter(Boolean).join(" · ")
}

// A click born on a control inside the row (the LinkedIn link, a source's popover trigger,
// the actions) is theirs; so is one that bubbled in from a portal (a dialog, a popover), which
// React routes through the row without the row's DOM ever holding it.
function onControl(event: MouseEvent<HTMLElement>): boolean {
  if (!(event.target instanceof Element)) return false
  return !event.currentTarget.contains(event.target) || event.target.closest("a, button, input") !== null
}

/**
 * One person as rendering.py _candidate_row lays them out: who they are on the left, the tags
 * and pin over them, their sources and who they came through under them; the overall score
 * with its reasoning and the judges' labels on the right, Score over them. A click on the row
 * opens them in the drawer, or closes it when they are open. `group/row` shows the tag
 * trigger and pin on hover.
 */
export const ResultRow = memo(function ResultRow({
  result,
  ranked,
  labels,
  open,
  focused,
  context,
  onToggle,
}: ResultRowProps) {
  const { row, candidate } = result
  return (
    <div
      className="result-row group/row"
      data-person-id={row.person_id}
      data-result-key={result.key}
      data-open={open}
      data-focus={focused}
    >
      {/* Keys live on the table (hooks/useResultKeys: j/k, Enter, t, s); a row is not a tab stop (W3). */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/interactive-supports-focus -- W3 */}
      <div
        className="result-line focus-bar"
        role="row"
        data-focus={focused}
        onClick={(event) => {
          if (!onControl(event)) onToggle(result.key)
        }}
      >
        <div className="result-main">
          {candidate ? (
            <span className="result-tags">
              <RowTags candidate={candidate} context={context} />
            </span>
          ) : null}
          <div className="result-person">
            <Avatar name={row.name} size={40} src={row.avatar_url || undefined} />
            <span className="result-who">
              <b>{row.name}</b>
              <span className="result-title">
                <LinkedInLink row={row} />
                {row.title || "Current role unknown"}
              </span>
              <small>{company(row)}</small>
              <NetworkSources attribution={candidate?.network_attribution ?? null} name={row.name} />
            </span>
          </div>
        </div>
        <div className="result-indicators">
          {candidate ? (
            <span className="result-actions">
              <RowScore result={result} candidate={candidate} context={context} />
            </span>
          ) : null}
          {ranked ? <Overall result={result} labels={labels} /> : <PondScores result={result} />}
        </div>
      </div>
    </div>
  )
})

// rendering.py _overall_indicator: the badge, the judges' reason, their labels under it. A
// person without an overall shows what happened instead ("Not judged"), never a zero.
function Overall({ result, labels }: { result: Result; labels: boolean }) {
  return (
    <div className="result-overall">
      {result.overall === null ? null : <ScoreCell result={result} ranked />}
      <div className="result-reason">
        <p className={result.overall === null ? "result-noscore" : undefined}>{result.reason}</p>
        <JudgeBadges candidate={result.candidate} shown={labels} />
      </div>
    </div>
  )
}

// rendering.py _pond_table: the pond's own score, then each trait with its reason.
function PondScores({ result }: { result: Result }) {
  return (
    <div className="result-overall">
      <ScoreCell result={result} ranked={false} />
      <div className="result-reason">
        {result.row.traits.length ? (
          <TraitList traits={result.row.traits} />
        ) : (
          <p className="result-noscore">No trait scores</p>
        )}
      </div>
    </div>
  )
}
