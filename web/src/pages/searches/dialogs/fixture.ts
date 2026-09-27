import type { Candidate, Ratings } from "@/types/searches"

// Synthetic: the rubric is human_ratings.RUBRIC, the person is made up.
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

export const CASEY: Candidate = {
  person_id: "casey",
  name: "Casey Delta",
  linkedin_url: "https://www.linkedin.com/in/casey-delta",
  title: "Staff engineer",
  company: "Acme",
  location: "",
  avatar_url: "",
  move_likelihood: null,
  why: "",
  found_run: "jordan-role",
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
