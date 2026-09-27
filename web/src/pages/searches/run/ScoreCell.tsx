import { Badge } from "@/components/ui/badge"

import { scoreBand, type ResultRow, type ScoreBand } from "../lib/ranking"

const BAND_VARIANT = { high: "ok", medium: "warn", low: "bad" } as const satisfies Record<ScoreBand, string>

interface ScoreCellProps {
  result: ResultRow
  // Ranked runs show the overall 1–5; a pond table shows the pond's own 0–1 score.
  ranked: boolean
}

// A missing overall shows what happened instead ("Not judged"), never a zero.
export function ScoreCell({ result, ranked }: ScoreCellProps) {
  if (!ranked) {
    const score = result.row.final_score
    return (
      <span className="result-score">
        <Badge variant={BAND_VARIANT[scoreBand(score)]}>{Math.round(score * 100)}%</Badge>
      </span>
    )
  }
  if (result.overall === null) {
    return (
      <span className="result-score">
        <span className="result-unscored">{result.reason}</span>
      </span>
    )
  }
  return (
    <span className="result-score" data-overall={result.overall}>
      <Badge variant={BAND_VARIANT[scoreBand(result.overall / 5)]}>{result.overall}/5</Badge>
    </span>
  )
}
