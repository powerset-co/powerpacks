import { Badge } from "@/components/ui/badge"
import { scoreBand, type ResultRow, type ScoreBand } from "@/lib/searches/ranking"

const BAND_VARIANT = { high: "ok", medium: "warn", low: "bad" } as const satisfies Record<ScoreBand, string>

// A ranked run's overall 1–5. A missing overall shows what happened instead ("Not judged"), never a zero.
export function ScoreCell({ result }: { result: ResultRow }) {
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
