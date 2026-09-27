// Search JSON shapes, field for field with results_web/model.py dataclasses.

// model.py:31-36 and 658 fix the rendered groups.
export type SearchGroupKey = "send_worthy" | "chat_worthy" | "wrong_timing_relationship" | "passed"

// model.py:166 validates these decisions.
export type PinDecision = "introduce" | "review" | "not_supported"

export interface SearchCard {
  run_id: string
  title: string
  company: string
  status: string
  created_at: string
  updated_at: string
  search_version: string
  candidates: number
  ponds_run: number
  cost_usd: number
  pinned: number
  ce_scored: number
  score_5: number
  score_4: number
  score_3: number
}

export interface TeamMember {
  name: string
  title: string
  linkedin_url: string
  location: string
  started_on: string
}

export interface TeamSimilarity {
  rank: number
  candidate_count: number
  score: number
  method: string
  closest_names: string[]
}

export interface TraitScore {
  name: string
  score: number
  confidence: number
  reason: string
  meaning: string
}

export interface Position {
  title: string
  company: string
  company_url: string
  start_date: string
  end_date: string
  is_current: boolean
  description: string
  headcount: number
  stage: string
  funding: number
}

export interface Education {
  school: string
  degree: string
  field_of_study: string
  start_year: number
  end_year: number
}

export interface PondCandidate {
  person_id: string
  title: string
  company: string
  location: string
  avatar_url: string
  final_score: number
  traits: TraitScore[]
  name: string
  linkedin_url: string
  reasoning: string
  vertical_sources: string[]
  matched_positions: number[]
  summary: string
  profile_location: string
  positions: Position[]
  education: Education[]
  source_channel: string
  source_operator: string
  cross_encoder_score: number | null
  cross_encoder_score_1_to_5: number | null
  cross_encoder_score_type: string
  cross_encoder_threshold: number | null
  cross_encoder_passed: boolean | null
  cross_encoder_status: string
}

export interface CandidatePond {
  run_id: string
  pond_n: number
  query: string
  candidate: PondCandidate
}

export interface MoveLikelihood {
  label: string
  why: string
}

export interface CandidateJudgment {
  domain_score: number | null
  opportunity_cap: number | null
  overall_score: number | null
  domain_reason: string
  opportunity_reason: string
  company_context: string
  model: string
  status: string
}

export interface PinJudgment {
  decision: PinDecision | null
  reason: string
  model: string
  status: string
}

export interface Pond {
  run_id: string
  pond_n: number
  query: string
  diagnosis: string
  move: string
  reviewed_count: number
  below_threshold: boolean
  result_count: number
  cost_usd: number
  candidates: PondCandidate[]
}

export interface NetworkSource {
  channel: string
  total_interactions: number
  operator_count: number
}

export interface GmailAccountDetail {
  email: string
  interactions: number
}

export interface NetworkOperator {
  operator_id: string
  operator_name: string
  channels: string[]
  gmail_interactions: number | null
  message_interactions: number | null
  gmail_account_details: GmailAccountDetail[]
}

export interface PersonAttribution {
  person_id: string
  sources: NetworkSource[]
  operators: NetworkOperator[]
  total_interactions: number
}

export interface Candidate {
  person_id: string
  name: string
  linkedin_url: string
  title: string
  company: string
  location: string
  avatar_url: string
  move_likelihood: MoveLikelihood | null
  why: string
  found_run: string
  found_pond: number
  found_query: string
  queries: string[]
  ponds: CandidatePond[]
  human_score: number | null
  human_note: string
  candidate_judgment: CandidateJudgment | null
  network_attribution: PersonAttribution | null
  taste_score: number | null
  pin_confidence: number | null
  pin_judgment: PinJudgment | null
  team_similarity: TeamSimilarity | null
}

export interface CandidateGroup {
  key: SearchGroupKey
  label: string
  candidates: Candidate[]
}

export interface SearchResult {
  run_id: string
  title: string
  company: string
  created_at: string
  total_cost_usd: number
  ponds: Pond[]
  groups: CandidateGroup[]
  jd_text: string
  candidates: Candidate[]
  team: TeamMember[]
  team_fetched_at: string
  team_status: string
}

export interface CatalogPayload {
  searches: SearchCard[]
}

// human_ratings.py:6-14 has numeric keys; JSON object keys are strings.
export interface Ratings {
  rubric: Record<string, string>
  legacy: Record<string, number>
}

export interface SearchRunPayload {
  search: SearchResult
  ratings: Ratings
}

export interface Tagged {
  tags: string[]
  assignments: Record<string, string[]>
}

export interface TagsPayload {
  tagged: Tagged | null
}
