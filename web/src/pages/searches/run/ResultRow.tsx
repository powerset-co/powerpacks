import { memo, type MouseEvent } from "react"

import { CompanyLogo, ProxiedLinkedinAvatar } from "@/components/shared"
import { currentRole } from "@/lib/searches/person"
import type { ResultRow as Result } from "@/lib/searches/ranking"

import { TraitList } from "./Evidence"
import { JudgeBadges } from "./JudgeBadges"
import { LinkedInLink } from "./LinkedInLink"
import { NetworkSources } from "./NetworkSources"
import { RowFlag, RowScore, RowTags, type RowContext } from "./RowActions"
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

// A click born on a control inside the row (the LinkedIn link, a source's popover trigger,
// the actions) is theirs; so is one that bubbled in from a portal (a dialog, a popover), which
// React routes through the row without the row's DOM ever holding it.
function onControl(event: MouseEvent<HTMLElement>): boolean {
  if (!(event.target instanceof Element)) return false
  return !event.currentTarget.contains(event.target) || event.target.closest("a, button, input") !== null
}

/**
 * One person: who they are on the left (name and sources, then current role · company from their
 * positions · location; the headline is the drawer's; beside a chat the sources sit at the
 * line's right end), the tags and pin over them; the scores and reasoning on the right. A ranked (job description) run puts Score over
 * the reasoning; a plain run puts the feedback flag there. A click on the row opens them in the
 * drawer, or closes it when they are open. `group/row` shows the row's controls on hover.
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
  const role = currentRole(row)
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
          <div className="result-person">
            <ProxiedLinkedinAvatar name={row.name} size={40} src={row.avatar_url || undefined} />
            <span className="result-who">
              <span className="result-name">
                <b>{row.name}</b>
                <LinkedInLink row={row} />
                {/* Over the person's top right when wide; at the end of the name's line beside a chat. */}
                {candidate ? (
                  <span className="result-tags">
                    <RowTags candidate={candidate} context={context} />
                  </span>
                ) : null}
                <NetworkSources attribution={candidate?.network_attribution ?? null} name={row.name} />
              </span>
              <span className="result-place">
                <RoleLine title={role.title} company={role.company} />
                {row.location ? <small className="result-location">{row.location}</small> : null}
              </span>
            </span>
          </div>
        </div>
        <div className="result-indicators" data-ranked={ranked}>
          {candidate ? (
            <span className="result-actions">
              {ranked ? (
                <RowScore result={result} candidate={candidate} context={context} />
              ) : (
                <RowFlag candidate={candidate} context={context} />
              )}
            </span>
          ) : null}
          <Score result={result} ranked={ranked} labels={labels} parentScores={context.parentScores} />
        </div>
      </div>
    </div>
  )
})

// Current role · company, whichever is known, else a plain note. The drawer passes `logo` to
// show the company's logo just before its name; the list rows leave it out.
export function RoleLine({ title, company, logo }: { title: string; company: string; logo?: string }) {
  if (!title && !company) return <span className="result-title result-unknown">Current role unknown</span>
  return (
    <span className="result-title">
      {title ? <span className="result-role">{title}</span> : null}
      {title && company ? <span className="result-at">·</span> : null}
      {company ? (
        <span className="result-company">
          {logo !== undefined ? <CompanyLogo company={company} src={logo || undefined} size={14} /> : null}
          <span>{company}</span>
        </span>
      ) : null}
    </span>
  )
}

// rendering.py _overall_indicator. A ranked row shows the one score the list is ranked by (the
// judges' 1–5) with the reasoning beside it and the judges' labels under it; a person without an
// overall shows what happened instead ("Not judged"), never a zero. A plain run's row shows what
// the cloud app's row shows: each trait's share with its name and reason, nothing else. The
// pond's own 0–1 only orders the list, and the overall reasoning is the drawer's.
interface ScoreProps {
  result: Result
  ranked: boolean
  labels: boolean
  parentScores: RowContext["parentScores"]
}

function Score({ result, ranked, labels, parentScores }: ScoreProps) {
  if (!ranked) {
    return (
      <div className="result-overall">
        {parentScores ? (
          <ScoreCell result={result} ranked={false} parentScore={parentScores.get(result.row.person_id)} />
        ) : null}
        <div className="result-reason">
          <TraitList traits={result.row.traits} />
        </div>
      </div>
    )
  }
  const unscored = result.overall === null
  return (
    <div className="result-overall">
      {unscored ? null : <ScoreCell result={result} ranked={ranked} />}
      <div className="result-reason">
        <p className={unscored ? "result-noscore" : undefined}>{result.reason}</p>
        <JudgeBadges candidate={result.candidate} shown={labels} />
      </div>
    </div>
  )
}
