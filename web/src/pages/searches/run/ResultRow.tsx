import { memo, type ReactNode } from "react"

import { Avatar, SourcePills } from "@/components/shared"
import { usePresence } from "@/hooks/usePresence"
import { toChannels } from "@/lib/channels"
import { plural } from "@/lib/people/copy"
import type { ResultRow as Result } from "@/lib/searches/ranking"
import type { PondCandidate } from "@/types/searches"

import { Evidence } from "./Evidence"
import { JudgeBadges } from "./JudgeBadges"
import { Operators } from "./Operators"
import { ScoreCell } from "./ScoreCell"

interface ResultRowProps {
  result: Result
  ranked: boolean
  labels: boolean
  expanded: boolean
  focused: boolean
  actions: ReactNode
  onToggle: (key: string) => void
}

function headline(row: PondCandidate): string {
  return [row.title, row.company].filter(Boolean).join(" · ") || "Current role unknown"
}

function roles(row: PondCandidate): string {
  if (!row.positions.length) return "—"
  const matched = row.matched_positions.filter((index) => index < row.positions.length).length
  return matched
    ? `${plural(row.positions.length, "role")} · ${matched} matched`
    : plural(row.positions.length, "role")
}

// One person: the button line toggles the evidence below it; the actions slot sits outside
// the button so its own controls stay separate. `group/row` shows the tag trigger on hover.
export const ResultRow = memo(function ResultRow({
  result,
  ranked,
  labels,
  expanded,
  focused,
  actions,
  onToggle,
}: ResultRowProps) {
  const { row, candidate } = result
  const evidence = usePresence(expanded ? result : null)
  const attribution = candidate?.network_attribution
  const channels = toChannels([...new Set(attribution?.sources.map((source) => source.channel))])
  return (
    <div
      className="result-row group/row"
      data-person-id={row.person_id}
      data-result-key={result.key}
      data-expanded={expanded}
      data-focus={focused}
    >
      <div className="result-line">
        <button
          type="button"
          className="result-main"
          aria-expanded={expanded}
          aria-controls={evidence.mounted ? `evidence-${result.key}` : undefined}
          onClick={() => onToggle(result.key)}
        >
          <span className="result-person">
            <Avatar name={row.name} size={26} src={row.avatar_url || undefined} />
            <span className="result-who">
              <b>{row.name}</b>
              <small>{headline(row)}</small>
            </span>
          </span>
          <ScoreCell result={result} ranked={ranked} />
          <JudgeBadges candidate={candidate} shown={labels} />
          {channels.length ? (
            <SourcePills className="result-sources" size="sm" channels={channels} />
          ) : (
            <span className="result-sources result-none">—</span>
          )}
          <Operators operators={attribution?.operators ?? []} />
          <span className="result-roles">{roles(row)}</span>
          <i className="result-chevron" aria-hidden="true" />
        </button>
        <span className="result-actions">{actions}</span>
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
