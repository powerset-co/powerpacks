import { Chip } from "@/components/shared"
import { SCORES, type Score } from "@/lib/searches/filters"
import { toggled } from "@/lib/sets"

import { GROUP_LABEL } from "./styles"

interface ScoreFilterProps {
  scores: ReadonlySet<Score>
  onChange: (scores: ReadonlySet<Score>) => void
}

// "Overall: All scores 1 2 3 4 5"; any number of scores at once, none means all.
export function ScoreFilter({ scores, onChange }: ScoreFilterProps) {
  return (
    <span className="inline-flex items-center gap-1.5" role="group" aria-label="Overall score filter">
      <span className={GROUP_LABEL}>Overall:</span>
      <Chip pressed={!scores.size} onClick={() => onChange(new Set())}>
        All scores
      </Chip>
      {SCORES.map((score) => (
        <Chip
          key={score}
          pressed={scores.has(score)}
          aria-label={`Overall score ${score}`}
          className="min-w-7 justify-center"
          onClick={() => onChange(toggled(scores, score))}
        >
          {score}
        </Chip>
      ))}
    </span>
  )
}
