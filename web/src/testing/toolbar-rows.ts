// Synthetic toolbar rows for the results toolbar suites (lib/searches, pages/searches/toolbar).

import type { ToolbarRow } from "@/lib/searches/filters"
import type { Candidate, NetworkOperator, PondCandidate } from "@/types/searches"

export function operator(operator_id: string, operator_name: string): NetworkOperator {
  return {
    operator_id,
    operator_name,
    channels: ["gmail"],
    gmail_interactions: 3,
    message_interactions: null,
    gmail_account_details: [],
  }
}

interface RowFields {
  overall?: number | null
  human?: number | null
  operators?: NetworkOperator[]
  linkedin?: string
  reason?: string
  pond?: Partial<PondCandidate>
}

/** One row for `person_id`; a candidate record only when it has a human score or operators. */
export function toolbarRow(person_id: string, name: string, fields: RowFields = {}): ToolbarRow {
  const row: PondCandidate = {
    person_id,
    name,
    title: "Software Engineer",
    company: "Example Labs",
    location: "Oakland, CA",
    avatar_url: "",
    final_score: 0.8,
    traits: [],
    linkedin_url: fields.linkedin ?? `https://linkedin.com/in/${person_id}`,
    reasoning: "",
    vertical_sources: [],
    matched_positions: [],
    summary: "",
    profile_location: "",
    positions: [],
    education: [],
    source_channel: "gmail",
    source_operator: "Alex Operator",
    cross_encoder_score: 3,
    cross_encoder_score_1_to_5: 3,
    cross_encoder_score_type: "expected_rating_1_to_5",
    cross_encoder_threshold: null,
    cross_encoder_passed: null,
    cross_encoder_status: "ok",
    ...fields.pond,
  }
  const operators = fields.operators ?? []
  const candidate: Candidate = {
    person_id,
    name,
    linkedin_url: row.linkedin_url,
    title: row.title,
    company: row.company,
    location: row.location,
    avatar_url: "",
    move_likelihood: null,
    why: "",
    found_run: "jordan-role",
    found_pond: 1,
    found_query: "",
    queries: [],
    ponds: [],
    human_score: fields.human ?? null,
    human_note: "",
    candidate_judgment: null,
    network_attribution: operators.length
      ? { person_id, sources: [], operators, total_interactions: 3 }
      : null,
    taste_score: null,
    pin_confidence: null,
    pin_judgment: null,
    team_similarity: null,
  }
  return { row, candidate, overall: fields.overall ?? null, reason: fields.reason ?? "Relevant work" }
}
