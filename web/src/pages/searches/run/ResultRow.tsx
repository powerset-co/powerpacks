import { memo, type MouseEvent } from "react"

import { Avatar, SourcePills } from "@/components/shared"
import { LinkedInIcon } from "@/components/shared/icons/channels"
import { usePresence } from "@/hooks/usePresence"
import { plural } from "@/lib/copy"
import type { ResultRow as Result } from "@/lib/searches/ranking"
import { sourceFamilies } from "@/lib/searches/sources"
import type { PondCandidate } from "@/types/searches"

import { Evidence } from "./Evidence"
import { JudgeBadges } from "./JudgeBadges"
import { Operators } from "./Operators"
import { RowActions, type RowContext } from "./RowActions"
import { ScoreCell } from "./ScoreCell"

interface ResultRowProps {
  result: Result
  ranked: boolean
  labels: boolean
  expanded: boolean
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

// One person: a click on the line toggles the evidence below it. `group/row` shows the tag
// trigger on hover.
export const ResultRow = memo(function ResultRow({
  result,
  ranked,
  labels,
  expanded,
  focused,
  context,
  onToggle,
}: ResultRowProps) {
  const { row, candidate } = result
  const evidence = usePresence(expanded ? result : null)
  const attribution = candidate?.network_attribution
  const families = sourceFamilies(attribution ?? null)
  return (
    <div
      className="result-row group/row"
      data-person-id={row.person_id}
      data-result-key={result.key}
      data-expanded={expanded}
      data-focus={focused}
    >
      {/* Keys live on the table (hooks/useResultKeys: j/k, Enter, t, s); a row is not a tab stop (W3). */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/interactive-supports-focus -- W3 */}
      <div
        className="result-line focus-bar"
        role="row"
        data-focus={focused}
        aria-expanded={expanded}
        aria-controls={evidence.mounted ? `evidence-${result.key}` : undefined}
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
                {row.linkedin_url ? (
                  <a
                    className="result-linkedin"
                    href={row.linkedin_url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`${row.name} on LinkedIn`}
                  >
                    <LinkedInIcon />
                  </a>
                ) : null}
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
          <i className="result-chevron chevron" data-open={expanded} aria-hidden="true" />
        </div>
        <span className="result-actions">
          {candidate ? <RowActions result={result} candidate={candidate} context={context} /> : null}
        </span>
      </div>
      {evidence.mounted ? (
        <Evidence
          result={result}
          ranked={ranked}
          open={evidence.open}
          onTransitionEnd={evidence.onTransitionEnd}
        />
      ) : null}
    </div>
  )
})
