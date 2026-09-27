import { memo, type MouseEvent } from "react"

import { Avatar, SourcePills } from "@/components/shared"
import { plural } from "@/lib/copy"
import type { ResultRow as Result } from "@/lib/searches/ranking"
import { sourceFamilies } from "@/lib/searches/sources"
import type { PondCandidate } from "@/types/searches"

import { JudgeBadges } from "./JudgeBadges"
import { LinkedInLink } from "./LinkedInLink"
import { Operators } from "./Operators"
import { RowActions, type RowContext } from "./RowActions"
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

function headline(row: PondCandidate): string {
  return [row.title, row.company, row.location].filter(Boolean).join(" · ") || "Current role unknown"
}

function roles(row: PondCandidate): string {
  if (!row.positions.length) return "—"
  const matched = row.matched_positions.filter((index) => index < row.positions.length).length
  return matched
    ? `${plural(row.positions.length, "role")} · ${matched} matched`
    : plural(row.positions.length, "role")
}

// A click born on a control inside the row (the LinkedIn link, the actions, a dialog) is theirs.
function onControl(event: MouseEvent): boolean {
  return event.target instanceof Element && event.target.closest("a, button, input, [role='dialog']") !== null
}

// One person: a click on the line opens them in the drawer, or closes it when they are open.
// `group/row` shows the tag trigger on hover.
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
  const attribution = candidate?.network_attribution
  const families = sourceFamilies(attribution ?? null)
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
          <span className="result-person">
            <Avatar name={row.name} size={26} src={row.avatar_url || undefined} />
            <span className="result-who">
              <b>{row.name}</b>
              <small>
                <LinkedInLink row={row} />
                {headline(row)}
              </small>
            </span>
          </span>
          <ScoreCell result={result} ranked={ranked} />
          <JudgeBadges candidate={candidate} shown={labels} />
          {families.length ? (
            <SourcePills
              className="result-sources"
              size="sm"
              channels={families.map((family) => family.channel)}
              counts={Object.fromEntries(families.map((family) => [family.channel, family.count]))}
            />
          ) : (
            <span className="result-sources result-none">—</span>
          )}
          <Operators operators={attribution?.operators ?? []} />
          <span className="result-roles">{roles(row)}</span>
        </div>
        <span className="result-actions">
          {candidate ? <RowActions result={result} candidate={candidate} context={context} /> : null}
        </span>
      </div>
    </div>
  )
})
