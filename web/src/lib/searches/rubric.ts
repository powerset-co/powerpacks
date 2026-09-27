import type { Ratings } from "@/types/searches"

export interface RubricChoice {
  score: number
  /** "Strong yes", the part before the dash. */
  name: string
  /** "Particularly compelling", the part after it. */
  detail: string
  meaning: string
}

const SEPARATOR = " — "

/** human_ratings.RUBRIC as choices, lowest score first. */
export function rubricChoices(rubric: Ratings["rubric"]): RubricChoice[] {
  return Object.entries(rubric)
    .map(([score, meaning]) => {
      const [name = meaning, ...rest] = meaning.split(SEPARATOR)
      return { score: Number(score), name, detail: rest.join(SEPARATOR), meaning }
    })
    .sort((a, b) => a.score - b.score)
}
