import { Badge } from "@/components/ui/badge"
import { scoreBand, type ResultRow, type ScoreBand } from "@/lib/searches/ranking"

const BAND_VARIANT = { high: "ok", medium: "warn", low: "bad" } as const satisfies Record<ScoreBand, string>

interface ScoreCellProps {
  result: ResultRow
  // Ranked runs show the overall 1–5; a pond table shows the pond's own 0–1 score.
  ranked: boolean
  // A refined run's score for this person on the step before; none for a row new to this step.
  parentScore?: number
}

// A missing overall shows what happened instead ("Not judged"), never a zero.
export function ScoreCell({ result, ranked, parentScore }: ScoreCellProps) {
  if (!ranked) {
    const score = result.row.final_score
    const delta = parentScore === undefined ? 0 : Math.round(score * 100) - Math.round(parentScore * 100)
    return (
      <span className="result-score">
        <Badge variant={BAND_VARIANT[scoreBand(score)]}>{Math.round(score * 100)}%</Badge>
        {delta === 0 ? null : (
          <small className="result-delta" data-delta={delta > 0 ? "up" : "down"}>
            {delta > 0 ? `▲ ${delta}` : `▼ ${-delta}`}
          </small>
        )}
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
