// Synthetic searches for tests: the catalog and one run as GET /searches/api/* return them,
// single result rows for the filter and export suites, and a stand-in for localStorage.

import type { ResultRow } from "@/lib/searches/ranking"
import type {
  Candidate,
  CandidateJudgment,
  NetworkOperator,
  Pond,
  PondCandidate,
  Ratings,
  SearchCard,
  SearchRunPayload,
} from "@/types/searches"

export const RUN_ID = "jordan-role"

// The rubric is human_ratings.RUBRIC and LEGACY_SCORES, as the server sends them.
export const RATINGS: Ratings = {
  rubric: {
    "1": "Clear no — Wrong role or clearly lacks required experience",
    "2": "Lean no — Some relevant experience, but I wouldn’t share them",
    "3": "Borderline — Concerns around fit, but worth sharing",
    "4": "Yes — Qualified and relevant",
    "5": "Strong yes — Particularly compelling",
  },
  legacy: { "1": 1, "2": 2, "3": 2, "4": 2, "7": 3, "8": 4, "9": 5, "10": 5 },
}
export const QUERIES = ["Backend engineers in Oakland", "Platform engineers who shipped payments"] as const

function card(fields: Partial<SearchCard> & Pick<SearchCard, "run_id" | "title">): SearchCard {
  return {
    company: "Example Labs",
    status: "awaiting_diagnosis",
    created_at: "2026-09-26T09:00:00Z",
    updated_at: "2026-09-26T09:00:00Z",
    search_version: "2026-09-26",
    candidates: 6,
    ponds_run: 2,
    cost_usd: 1.25,
    pinned: 0,
    ce_scored: 6,
    score_5: 1,
    score_4: 1,
    score_3: 0,
    ...fields,
  }
}

// Newest first, as the server sorts them.
export const CARDS: SearchCard[] = [
  card({ run_id: RUN_ID, title: "Backend Engineer" }),
  card({
    run_id: "casey-role",
    title: "Design Lead",
    company: "Sample Co",
    status: "running",
    created_at: "2026-09-25T09:00:00Z",
  }),
  card({
    run_id: "morgan-role",
    title: "Data Scientist",
    created_at: "2026-09-10T09:00:00Z",
    search_version: "2026-09-01",
  }),
  card({
    run_id: "riley-role",
    title: "Founding Engineer",
    company: "Sample Co",
    created_at: "2025-12-02T09:00:00Z",
    search_version: "",
  }),
]

function row(
  fields: Partial<PondCandidate> & Pick<PondCandidate, "person_id" | "name" | "cross_encoder_score">,
): PondCandidate {
  return {
    title: "Software Engineer",
    company: "Example Labs",
    location: "Oakland, CA",
    avatar_url: "",
    final_score: 0.8,
    traits: [
      { name: "Backend depth", score: 0.9, confidence: 0.8, reason: "Built the API layer.", meaning: "" },
    ],
    linkedin_url: "",
    reasoning: "Leads the current reliability platform.",
    vertical_sources: ["title"],
    matched_positions: [0],
    summary: "",
    profile_location: "",
    positions: [
      {
        title: "Staff Engineer",
        company: "Example Labs",
        company_url: "",
        start_date: "2022-01-01",
        end_date: "",
        is_current: true,
        description: "",
        headcount: 120,
        stage: "Series B",
        funding: 40_000_000,
      },
      {
        title: "Software Engineer",
        company: "Sample Co",
        company_url: "",
        start_date: "2018-03-01",
        end_date: "2021-12-01",
        is_current: false,
        description: "",
        headcount: 0,
        stage: "",
        funding: 0,
      },
    ],
    education: [
      {
        school: "Example University",
        degree: "BS",
        field_of_study: "Computer Science",
        start_year: 2010,
        end_year: 2014,
      },
    ],
    source_channel: "",
    source_operator: "",
    cross_encoder_score_1_to_5: null,
    cross_encoder_score_type: "expected_rating_1_to_5",
    cross_encoder_threshold: null,
    cross_encoder_passed: null,
    cross_encoder_status: "ok",
    ...fields,
  }
}

function rating(person_id: string, name: string, score: number, fields: Partial<PondCandidate> = {}) {
  return row({ person_id, name, cross_encoder_score: score, cross_encoder_score_1_to_5: score, ...fields })
}

function qualification(person_id: string, name: string, score: number, passed: boolean) {
  return row({
    person_id,
    name,
    cross_encoder_score: score,
    cross_encoder_score_type: "qualification_score",
    cross_encoder_threshold: 0.3,
    cross_encoder_passed: passed,
  })
}

const JORDAN = rating("p-jordan", "Jordan Bravo", 4.6, { title: "Staff Engineer" })
const CASEY = rating("p-casey", "Casey Delta", 4.1)
const MORGAN = rating("p-morgan", "Morgan Echo", 2.4)
const RILEY = qualification("p-riley", "Riley Foxtrot", 0.85, true)
const AVERY = qualification("p-avery", "Avery Golf", 0.31, true)
const QUINN = qualification("p-quinn", "Quinn Hotel", 0.15, false)
// Casey again in the second pond, screened lower: the table keeps the first pond's row.
const CASEY_AGAIN = rating("p-casey", "Casey Delta", 3.2, { reasoning: "Shipped the payments ledger." })

function pond(pond_n: number, candidates: PondCandidate[], fields: Partial<Pond> = {}): Pond {
  return {
    run_id: RUN_ID,
    pond_n,
    query: QUERIES[pond_n - 1] ?? "",
    diagnosis: "",
    move: "",
    reviewed_count: 0,
    below_threshold: false,
    result_count: 40 * pond_n,
    cost_usd: 0.5,
    candidates,
    ...fields,
  }
}

function judgment(overall: number, reason: string): CandidateJudgment {
  return {
    domain_score: overall,
    opportunity_cap: 5,
    overall_score: overall,
    domain_reason: reason,
    opportunity_reason: "Opportunity fits.",
    company_context: "",
    model: "test",
    status: "ok",
  }
}

function candidate(source: PondCandidate, fields: Partial<Candidate> = {}): Candidate {
  return {
    person_id: source.person_id,
    name: source.name,
    linkedin_url: "",
    title: source.title,
    company: source.company,
    location: source.location,
    avatar_url: "",
    move_likelihood: null,
    why: "",
    found_run: RUN_ID,
    found_pond: 1,
    found_query: QUERIES[0],
    queries: [QUERIES[0]],
    ponds: [],
    human_score: null,
    human_note: "",
    candidate_judgment: null,
    network_attribution: null,
    taste_score: null,
    pin_confidence: null,
    pin_judgment: null,
    team_similarity: null,
    ...fields,
  }
}

export const RUN: SearchRunPayload = {
  search: {
    run_id: RUN_ID,
    title: "Backend Engineer",
    company: "Example Labs",
    created_at: "2026-09-26T09:00:00Z",
    total_cost_usd: 1.25,
    ponds: [
      pond(1, [JORDAN, CASEY, MORGAN, RILEY], { reviewed_count: 3 }),
      pond(2, [AVERY, QUINN, CASEY_AGAIN]),
    ],
    groups: [],
    jd_text: "Build the payments platform with a small backend team.",
    candidates: [
      candidate(JORDAN, {
        linkedin_url: "https://www.linkedin.com/in/jordan-bravo",
        candidate_judgment: judgment(5, "Built this exact system twice."),
        taste_score: 4.5,
        pin_confidence: 82,
        pin_judgment: { decision: "introduce", reason: "Strong fit.", model: "test", status: "ok" },
        team_similarity: {
          rank: 2,
          candidate_count: 6,
          score: 0.81,
          method: "embedding",
          closest_names: ["Sam India"],
        },
        network_attribution: {
          person_id: JORDAN.person_id,
          sources: [
            { channel: "gmail", total_interactions: 42, operator_count: 1 },
            { channel: "linkedin", total_interactions: 0, operator_count: 2 },
          ],
          operators: [
            {
              operator_id: "op-1",
              operator_name: "Drew Kilo",
              channels: ["gmail", "linkedin"],
              gmail_interactions: 42,
              message_interactions: null,
              gmail_account_details: [],
            },
            {
              operator_id: "op-2",
              operator_name: "Emery Lima",
              channels: ["linkedin"],
              gmail_interactions: null,
              message_interactions: null,
              gmail_account_details: [],
            },
          ],
          total_interactions: 42,
        },
      }),
      // Sources the People page has no row for: X and a contacts export.
      candidate(CASEY, {
        network_attribution: {
          person_id: CASEY.person_id,
          sources: [
            { channel: "twitter", total_interactions: 0, operator_count: 1 },
            { channel: "csv_import", total_interactions: 0, operator_count: 1 },
          ],
          operators: [],
          total_interactions: 0,
        },
      }),
      candidate(MORGAN),
      candidate(RILEY, { candidate_judgment: judgment(4, "Ran payments infrastructure.") }),
      candidate(AVERY),
      candidate(QUINN),
    ],
    team: [
      {
        name: "Sam India",
        title: "Engineering Manager",
        linkedin_url: "",
        location: "Oakland, CA",
        started_on: "2023-04-01",
      },
    ],
    team_fetched_at: "2026-09-20T00:00:00Z",
    team_status: "",
  },
  ratings: RATINGS,
}

// The documented order: Jev scores first, then ratings; overall, then the screen score.
export const RANKED_NAMES = [
  ["Riley Foxtrot", "Avery Golf", "Quinn Hotel"],
  ["Jordan Bravo", "Morgan Echo", "Casey Delta"],
] as const

// One candidate on its own, as the score dialog shows it.
export const CASEY_CANDIDATE: Candidate = {
  person_id: "casey",
  name: "Casey Delta",
  linkedin_url: "https://www.linkedin.com/in/casey-delta",
  title: "Staff engineer",
  company: "Acme",
  location: "",
  avatar_url: "",
  move_likelihood: null,
  why: "",
  found_run: RUN_ID,
  found_pond: 1,
  found_query: "",
  queries: [],
  ponds: [],
  human_score: null,
  human_note: "",
  candidate_judgment: null,
  network_attribution: null,
  taste_score: null,
  pin_confidence: null,
  pin_judgment: null,
  team_similarity: null,
}

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

/** One ranked row for `person_id`, with its candidate record. */
export function resultRow(person_id: string, name: string, fields: RowFields = {}): ResultRow {
  const pondRow = rating(person_id, name, 3, {
    linkedin_url: fields.linkedin ?? `https://linkedin.com/in/${person_id}`,
    source_channel: "gmail",
    source_operator: "Alex Operator",
    ...fields.pond,
  })
  const operators = fields.operators ?? []
  return {
    key: `0:${person_id}`,
    row: pondRow,
    candidate: candidate(pondRow, {
      linkedin_url: pondRow.linkedin_url,
      human_score: fields.human ?? null,
      network_attribution: operators.length
        ? { person_id, sources: [], operators, total_interactions: 3 }
        : null,
    }),
    overall: fields.overall ?? null,
    reason: fields.reason ?? "Relevant work",
  }
}

// Node 25 defines its own global localStorage, which shadows jsdom's and has no methods unless
// node runs with --localstorage-file; the feedback suites stub this in-memory one instead.
export class MemoryStorage implements Storage {
  private items = new Map<string, string>()

  get length(): number {
    return this.items.size
  }

  clear(): void {
    this.items.clear()
  }

  getItem(key: string): string | null {
    return this.items.get(key) ?? null
  }

  key(index: number): string | null {
    return [...this.items.keys()][index] ?? null
  }

  removeItem(key: string): void {
    this.items.delete(key)
  }

  setItem(key: string, value: string): void {
    this.items.set(key, value)
  }
}
