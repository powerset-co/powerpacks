import type { Candidate, PondCandidate, Position } from "@/types/searches"

export interface CurrentRole {
  title: string
  company: string
  logo: string
}

/**
 * Where a person works now, from their positions rather than their headline: the first current
 * position, else the run's own current title and company (which persist_search_results built
 * from the same positions), with the logo of the position at that company.
 */
export function currentRole(row: PondCandidate): CurrentRole {
  const current: Position | undefined = row.positions.find((position) => position.is_current)
  if (current && (current.title || current.company)) {
    return { title: current.title, company: current.company, logo: current.logo_url }
  }
  const atCompany = row.company
    ? row.positions.find((position) => position.company === row.company && position.logo_url)
    : undefined
  return { title: row.title, company: row.company, logo: atCompany?.logo_url ?? "" }
}

/** "Title · Company", or whichever of the two is known. */
export function roleLine(role: CurrentRole): string {
  return [role.title, role.company].filter(Boolean).join(" · ")
}

/** The headline when it says more than the role line (many headlines are just "Title at Company"). */
export function shownHeadline(row: PondCandidate, role: CurrentRole): string {
  const headline = row.headline.trim()
  if (!headline) return ""
  const plain = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, "")
  const said = plain(headline)
  const asRole = [plain(roleLine(role)), plain([role.title, role.company].filter(Boolean).join(" at "))]
  return asRole.includes(said) ? "" : headline
}

/** Whether the judges gave this candidate a label the row shows (JudgeBadges): taste, a
 *  suggested pin, or a team-similarity rank. */
export function hasJudgeLabel(candidate: Candidate | undefined): boolean {
  if (!candidate) return false
  return (
    (candidate.candidate_judgment !== null && candidate.taste_score !== null) ||
    candidate.pin_judgment?.decision === "introduce" ||
    Boolean(candidate.team_similarity)
  )
}
