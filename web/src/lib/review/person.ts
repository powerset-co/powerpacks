// What a person card shows of a parent and one candidate: the name, the profile link, the
// summary, the avatar's initials, and the folded fact and label lists.

import { UNNAMED } from "@/lib/review/copy"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

const VISIBLE_FACTS = 3
const VISIBLE_LABELS = 3
const LABEL_SEPARATOR = " · "
/** The profile fetch writes this for a missing headline. */
const NO_HEADLINE = "--"

/** The card's name: the candidate's full name, else the parent's. A person-only card (several
 *  candidates) names the parent. */
export function displayName(
  person: ReviewPerson,
  candidate: ReviewCandidate | null,
  personOnly: boolean,
): string {
  if (!personOnly && candidate?.name) return candidate.name
  return person.name || UNNAMED
}

/** The "View LinkedIn" link: a fetched candidate's URL. A researched profile has none, and a
 *  person-only card leaves the link to each option. */
export function profileUrl(candidate: ReviewCandidate | null, personOnly: boolean): string {
  if (personOnly || !candidate || candidate.synthetic) return ""
  return candidate.url
}

/** The Summary fact: the headline, unless the profile had none. */
export function summaryOf(candidate: ReviewCandidate): string {
  return candidate.headline === NO_HEADLINE ? "" : candidate.headline
}

/**
 * The name whose initials the avatar draws: the candidate's, else the parent's, cut to its
 * runs of letters and digits. Initials come from the first and last run, so "Jordan O'Bravo"
 * draws JB; the shared Avatar splits on spaces alone.
 */
export function avatarName(person: ReviewPerson, candidate: ReviewCandidate | null): string {
  const words = (name: string) => (name.match(/[A-Za-z0-9]+/g) ?? []).join(" ")
  if (candidate?.name) return words(candidate.name)
  return words(person.name)
}

export interface Folded {
  shown: string[]
  rest: string[]
}

/** A Work / Education list: blank entries dropped, three shown, the rest behind "show more". */
export function foldFacts(items: readonly string[]): Folded {
  const values = items.filter((item) => item.trim())
  return { shown: values.slice(0, VISIBLE_FACTS), rest: values.slice(VISIBLE_FACTS) }
}

/** Label badges: three shown, the rest behind "+N". */
export function foldLabels(labels: readonly string[]): Folded {
  return { shown: labels.slice(0, VISIBLE_LABELS), rest: labels.slice(VISIBLE_LABELS) }
}

/** The "+N" badge's tooltip. */
export function labelTooltip(rest: readonly string[]): string {
  return rest.join(LABEL_SEPARATOR)
}
