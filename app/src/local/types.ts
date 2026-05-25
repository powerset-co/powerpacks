import type { DatabaseRecord, Education, TraitScore, WorkExperience } from "@/types/search";

export interface LocalRunSummary {
  taskId: string;
  conversationId?: string;
  fileName: string;
  query: string;
  status: string;
  task?: string;
  createdAt?: string | null;
  updatedAt?: string | null;
  mtimeMs: number;
  rowCount?: number | null;
  hydratedCount?: number | null;
  hasArtifacts: boolean;
  artifactDir?: string | null;
}

export interface LocalRunDetail extends LocalRunSummary {
  constraints?: Record<string, unknown>;
  steps?: Array<{
    id?: string;
    status?: string;
    recorded_at?: string;
    elapsed_ms?: number;
    output?: Record<string, unknown>;
  }>;
  artifacts?: Record<string, unknown>;
  resultCount: number;
}

export interface RawPowerpackResult {
  rank?: number | string;
  person_id?: string;
  name?: string;
  headline?: string;
  location?: string;
  current_titles?: string;
  current_companies?: string;
  linkedin_url?: string;
  hydrated?: boolean;
  source_run?: string;
  source_query?: string;
  final_score?: number | string;
  pre_rerank_score?: number | string;
  trait_scores?: string | Record<string, TraitScore>;
  overall_reasoning?: string;
  matched_position_indexes?: string | number[];
  matched_education_indexes?: string | number[];
  matched_profile_sections?: string | string[];
  vertical_sources?: string | string[];
  positions?: string | Array<Record<string, unknown>>;
  education?: string | Array<Record<string, unknown>>;
  reranked?: boolean;
  [key: string]: unknown;
}

export interface RawPowerpackProfile {
  person_id?: string;
  name?: string;
  headline?: string;
  linkedin_url?: string;
  location?: string;
  positions?: Array<Record<string, unknown>>;
  education?: Array<Record<string, unknown>>;
  matched_position_indexes?: number[];
  matched_education_indexes?: number[];
  matched_profile_sections?: string[];
  score?: number;
  base_score?: number;
  total_interactions?: number | null;
  vertical_sources?: string[];
  tech_skills?: string[];
  [key: string]: unknown;
}

export interface LocalRunResultsResponse {
  run: LocalRunDetail;
  rows: RawPowerpackResult[];
  profiles: Record<string, RawPowerpackProfile>;
  offset: number;
  limit: number;
  hasMore: boolean;
  totalRows: number;
}

function firstNonEmpty(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

function parseLinkedInPublicIdentifier(url?: string | null): string | undefined {
  if (!url) return undefined;
  const match = url.match(/linkedin\.com\/in\/([^/?#]+)/i);
  return match?.[1];
}

function parseJsonValue<T>(value: unknown, fallback: T): T {
  if (value == null || value === "") return fallback;
  if (typeof value !== "string") return value as T;
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

function normalizeTraitScores(value: unknown, fallbackScore: number, verticalSources: string[]): Record<string, TraitScore> {
  const parsed = parseJsonValue<Record<string, any>>(value, {});
  const entries = Object.entries(parsed || {});
  if (entries.length > 0) {
    return Object.fromEntries(entries.map(([traitName, raw]) => {
      const score = Number(raw?.score ?? raw?.confidence ?? 0);
      return [traitName, {
        score: Number.isFinite(score) ? Math.max(0, Math.min(1, score)) : 0,
        reason: raw?.reason || raw?.reasons?.[0] || "No reason provided",
        reasons: Array.isArray(raw?.reasons) ? raw.reasons : undefined,
        vertical_sources: Array.isArray(raw?.vertical_sources) ? raw.vertical_sources : verticalSources,
      } satisfies TraitScore];
    }));
  }

  const normalizedScore = Number.isFinite(fallbackScore) ? Math.max(0, Math.min(1, fallbackScore)) : 0;
  return {
    "search match": {
      score: normalizedScore,
      reason: `Powerpacks retrieval score ${fallbackScore ? fallbackScore.toFixed(4) : "0.0000"}`,
      vertical_sources: verticalSources,
    },
  };
}

function normalizeEducation(raw: Record<string, unknown>): Education {
  const startYear = raw.start_year ?? raw.startYear;
  const endYear = raw.end_year ?? raw.endYear;
  return {
    school_name: String(raw.school_name ?? raw.school ?? ""),
    school_linkedin_url: (raw.school_linkedin_url as string | null) ?? null,
    school_logo_url: (raw.school_logo_url as string | null) ?? null,
    degree: (raw.degree as string | null) ?? null,
    field_of_study: (raw.field_of_study as string | null) ?? null,
    grade: (raw.grade as string | null) ?? null,
    start_date: startYear ? `${startYear}` : null,
    end_date: endYear ? `${endYear}` : null,
  };
}

function normalizePosition(raw: Record<string, unknown>): WorkExperience {
  return {
    company_name: String(raw.company_name ?? raw.company ?? ""),
    company_urn: String(raw.company_id ?? raw.company_urn ?? ""),
    company_linkedin_url: (raw.company_linkedin_url as string | undefined) ?? undefined,
    company_domain: (raw.company_domain as string | undefined) ?? undefined,
    position_title: String(raw.position_title ?? raw.title ?? ""),
    department: (raw.department as string | null) ?? null,
    description: String(raw.description ?? raw.dense_text ?? ""),
    dense_text: (raw.dense_text as string | undefined) ?? undefined,
    location: (raw.location as string | null) ?? null,
    start_date: (raw.start_date as string | null) ?? null,
    end_date: (raw.end_date as string | null) ?? null,
    is_current: Boolean(raw.is_current),
    role_type: String(raw.role_track ?? raw.role_type ?? ""),
    emails: Array.isArray(raw.emails) ? raw.emails.map(String) : [],
  };
}

export function toDatabaseRecord(row: RawPowerpackResult, profile?: RawPowerpackProfile): DatabaseRecord {
  const linkedinUrl = firstNonEmpty(row.linkedin_url, profile?.linkedin_url);
  const personId = String(row.person_id ?? profile?.person_id ?? linkedinUrl ?? row.name ?? crypto.randomUUID());
  const score = Number(row.final_score ?? profile?.score ?? profile?.base_score ?? 0);
  const preRerankScore = Number(row.pre_rerank_score ?? profile?.score ?? profile?.base_score ?? 0);
  const rawPositions = parseJsonValue<Array<Record<string, unknown>> | undefined>(
    row.positions,
    Array.isArray(profile?.positions) ? profile.positions : undefined
  );
  const rawEducation = parseJsonValue<Array<Record<string, unknown>> | undefined>(
    row.education,
    Array.isArray(profile?.education) ? profile.education : undefined
  );
  const positions = Array.isArray(rawPositions) ? rawPositions.map(normalizePosition) : [];
  const education = Array.isArray(rawEducation) ? rawEducation.map(normalizeEducation) : [];
  const verticalSources = parseJsonValue<string[]>(row.vertical_sources, Array.isArray(profile?.vertical_sources) ? profile.vertical_sources : []);
  const traitScores = normalizeTraitScores(row.trait_scores, row.final_score != null ? score : preRerankScore, verticalSources);
  const matchedPositionIndexes = parseJsonValue<number[]>(row.matched_position_indexes, Array.isArray(profile?.matched_position_indexes) ? profile?.matched_position_indexes : []);
  const matchedEducationIndexes = parseJsonValue<number[]>(row.matched_education_indexes, Array.isArray(profile?.matched_education_indexes) ? profile.matched_education_indexes : []);
  const matchedProfileSections = parseJsonValue<string[]>(row.matched_profile_sections, Array.isArray(profile?.matched_profile_sections) ? profile.matched_profile_sections : []);

  return {
    name: String(row.name ?? profile?.name ?? "Unknown"),
    personId,
    title: String(row.headline ?? profile?.headline ?? row.current_titles ?? ""),
    location: String(row.location ?? profile?.location ?? ""),
    operatorId: "local",
    operatorStrength: Number(profile?.total_interactions ?? 0),
    positionSearchScore: row.final_score != null ? preRerankScore : score,
    locationSearchScore: 0,
    trait_scores: traitScores,
    overall_trait_score: score,
    result_index: Number(row.rank ?? 0) || undefined,
    linkedin_url: linkedinUrl,
    public_identifier: parseLinkedInPublicIdentifier(linkedinUrl),
    headline: String(row.headline ?? profile?.headline ?? ""),
    current_company: String(row.current_companies ?? ""),
    positions,
    education,
    matched_position_indexes: matchedPositionIndexes,
    matched_education_indexes: matchedEducationIndexes,
    matched_profile_sections: matchedProfileSections,
    overall_reasoning: typeof row.overall_reasoning === "string" ? row.overall_reasoning : undefined,
    rerank_reasoning: typeof row.overall_reasoning === "string" ? row.overall_reasoning : undefined,
    rerank_score: row.final_score != null ? score : undefined,
    vertical_sources: verticalSources,
  };
}
