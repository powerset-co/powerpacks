import { Badge } from "@/components/ui/badge"
import type { Candidate } from "@/types/searches"

interface JudgeBadgesProps {
  candidate: Candidate | undefined
  // The toolbar's Labels toggle; the column keeps its place while hidden.
  shown: boolean
}

// rendering.py _judge_badges: taste, the pin judge's suggestion and team similarity, none of
// which changes the score.
export function JudgeBadges({ candidate, shown }: JudgeBadgesProps) {
  const similarity = candidate?.team_similarity
  return (
    <span className="result-badges" data-labels={shown}>
      {candidate?.candidate_judgment ? (
        candidate.taste_score === null ? (
          <Badge variant="muted" title="No taste score on file">
            Taste N/A
          </Badge>
        ) : (
          <Badge variant="muted" title="Reporting taste score">
            Taste {candidate.taste_score.toFixed(1)}
          </Badge>
        )
      ) : null}
      {candidate?.pin_judgment?.decision === "introduce" ? (
        <Badge variant="info" title={`Pin confidence ${String(candidate.pin_confidence ?? "unscored")}/100`}>
          Suggested pin
        </Badge>
      ) : null}
      {similarity ? (
        <Badge
          variant="default"
          title={`Rank ${similarity.rank} of ${similarity.candidate_count} by likeness to the current team (${similarity.method}). Closest: ${similarity.closest_names.join(", ")}. Likeness is not a fit score.`}
        >
          Team #{similarity.rank}
        </Badge>
      ) : null}
    </span>
  )
}
