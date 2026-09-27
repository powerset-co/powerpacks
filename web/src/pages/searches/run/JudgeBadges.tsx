import { Badge } from "@/components/ui/badge"
import type { Candidate } from "@/types/searches"

interface JudgeBadgesProps {
  candidate: Candidate | undefined
  tags: readonly string[]
}

// rendering.py _judge_badges: taste, the pin judge's suggestion and team similarity, none of
// which changes the score; then the run's saved tags for this person.
export function JudgeBadges({ candidate, tags }: JudgeBadgesProps) {
  const similarity = candidate?.team_similarity
  return (
    <span className="result-badges">
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
      {tags.map((tag) => (
        <Badge key={tag} variant="muted" className="result-tag" data-tag={tag}>
          {tag}
        </Badge>
      ))}
    </span>
  )
}
