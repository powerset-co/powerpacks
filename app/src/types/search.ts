/**
 * Search result types — shared across components and services.
 *
 * Extracted from services/streamingClient.ts (2026-02-07).
 * The streaming client was dead code, but these types are used by
 * PowerSearchV2, DatabaseRecordTable, TraitScoreDisplay, PersonInfoCell.
 */

export interface StreamResponse {
  type: 'thinking' | 'generating' | 'chunk' | 'complete' | 'error' | 'update' | 'final';
  content?: string;
  error?: string;
  database_records?: DatabaseRecord[];
}

export interface TraitScore {
  score: number;
  reasons?: string[];
  reason?: string;
  vertical_sources?: string[];
}

/**
 * Structured trait with temporal scope and meaning metadata.
 *
 * Backend returns these from /expand. The user can toggle `temporal`
 * to "current" to enforce strict current-role matching during reranking.
 */
export type TraitTemporal = "current" | "past" | "all";
export type TraitMeaning = "role" | "experience" | "location" | "education" | "company" | "investor" | "general";

export interface Trait {
  value: string;
  temporal: TraitTemporal;
  meaning: TraitMeaning;
}

/**
 * Normalize a trait from the API — handles both legacy string format
 * and the new structured { value, temporal, meaning } format.
 */
export function normalizeTrait(raw: string | Trait): Trait {
  if (typeof raw === "string") {
    return { value: raw, temporal: "all", meaning: "general" };
  }
  return {
    value: raw.value ?? "",
    temporal: raw.temporal ?? "all",
    meaning: raw.meaning ?? "general",
  };
}

export function normalizeTraits(raw: (string | Trait)[]): Trait[] {
  return (raw ?? []).map(normalizeTrait);
}

/**
 * Extract trait scores from a JSONB blob.
 *
 * Two formats exist across different tables:
 * - query_results_v2.trait_scores: flat { "trait name": { score, reason } }
 * - agentic_interaction_results.trait_scores: nested { trait_scores: {...}, overall_reasoning, matched_position_indexes }
 *
 * This utility handles both and always returns Record<string, TraitScore>.
 */
export function extractTraitScores(blob: Record<string, any> | null | undefined): Record<string, TraitScore> {
  if (!blob || typeof blob !== 'object') return {};
  // Nested format (agentic_interaction_results): unwrap .trait_scores
  if (blob.trait_scores && typeof blob.trait_scores === 'object' && !('score' in blob.trait_scores)) {
    return blob.trait_scores as Record<string, TraitScore>;
  }
  // Flat format (query_results_v2): blob IS the trait map
  return blob as Record<string, TraitScore>;
}

/**
 * Extract matched_position_indexes — checks both the blob (nested format)
 * and falls back to a direct value.
 */
export function extractMatchedPositionIndexes(blob: Record<string, any> | null | undefined, direct?: number[]): number[] {
  if (Array.isArray(direct)) return direct;
  if (!blob) return [];
  if (Array.isArray(blob.matched_position_indexes)) return blob.matched_position_indexes;
  return [];
}

/**
 * Extract overall_reasoning — checks both the blob (nested format)
 * and falls back to a direct value.
 */
export function extractOverallReasoning(blob: Record<string, any> | null | undefined, direct?: string | null): string {
  if (direct) return direct;
  if (!blob) return '';
  if (typeof blob.overall_reasoning === 'string') return blob.overall_reasoning;
  return '';
}

export interface WorkExperience {
  company_name: string;
  company_urn: string;
  company_linkedin_url?: string;
  company_domain?: string;
  position_title: string;
  department: string | null;
  description: string;
  /** Role description from hydration API (dense_text field) */
  dense_text?: string;
  location: string | null;
  start_date: string | null;
  end_date: string | null;
  is_current: boolean;
  role_type: string;
  emails: string[];
}

export interface Education {
  school_name: string;
  school_linkedin_url: string | null;
  school_logo_url: string | null;
  degree: string | null;
  field_of_study: string | null;
  grade: string | null;
  start_date: string | null;
  end_date: string | null;
}

export interface DatabaseRecord {
  name: string;
  personId: string;
  title: string;
  location: string;
  operatorId: string;
  operatorStrength: number;
  positionSearchScore: number;
  locationSearchScore: number;
  trait_scores: Record<string, TraitScore>;
  overall_trait_score: number;
  result_index?: number;
  profile_picture_url?: string;
  linkedin_url?: string;
  public_identifier?: string;
  headline?: string;
  /** LinkedIn about/summary section */
  summary?: string;
  current_company?: string;
  positions?: WorkExperience[];
  education?: Education[];
  emails?: string[];
  matched_position_indexes?: number[];
  matched_education_indexes?: number[];
  matched_profile_sections?: string[];
  overall_reasoning?: string;
  vertical_sources?: string[];
  rerank_reasoning?: string | null;
  rerank_score?: number | null;
  reasoning_chain?: { step: number; type: string; query: string; reasoning: string; score: number }[];
  linkedin_followers?: number | null;
  linkedin_connections?: number | null;
  x_twitter_handle?: string | null;
  x_twitter_followers?: number | null;
  instagram_handle?: string | null;
}

export interface AIResponse {
  answer: string;
  databaseRecords: DatabaseRecord[];
}
