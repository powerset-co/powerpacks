// The cloud app's filter shape (network-search-app `searchServiceV2.ts`), kept for the Edit Filters
// modal copied from there, and the two mappings between it and a prepared search's filters: the
// modal edits entity items with an id and a display value; the expand payload holds plain names.

import type { FilterValue } from "./preparedSearch"

export interface EntityItem {
  id: string
  display_value: string
  type: string
  score?: number
  metadata?: Record<string, unknown> | null
}

export interface ExpandedFilters {
  company_ids: EntityItem[]
  role_ids: EntityItem[]
  education_ids: EntityItem[]
  investors: EntityItem[]
  cities: EntityItem[]
  states: EntityItem[]
  countries: EntityItem[]
  metro_areas: EntityItem[]
  macro_regions: EntityItem[]
  company_cities: EntityItem[]
  company_states: EntityItem[]
  company_countries: EntityItem[]
  company_metro_areas: EntityItem[]
  company_macro_regions: EntityItem[]
  funding_stage_min: string | null
  funding_stage_max: string | null
  funding_amount_min: number | null
  funding_amount_max: number | null
  headcount_min: number | null
  headcount_max: number | null
  last_funding_before: string | null
  last_funding_after: string | null
  valuation_min: number | null
  valuation_max: number | null
  founded_year_min: number | null
  founded_year_max: number | null
  yc_batches: EntityItem[]
  entity_types: EntityItem[]
  sector_types: EntityItem[]
  seniority_bands: EntityItem[]
  role_tracks: EntityItem[]
  tech_skills: EntityItem[]
  degree_levels: EntityItem[]
  fields_of_study: EntityItem[]
  graduation_year_min: number | null
  graduation_year_max: number | null
  years_experience_min: number | null
  years_experience_max: number | null
  age_min: number | null
  age_max: number | null
  position_after_date: string | null
  position_before_date: string | null
  role_semantic_query: string | null
  role_bm25_queries: string[] | null
  company_semantic_queries: EntityItem[]
  x_followers_min: number | null
  x_followers_max: number | null
  li_followers_min: number | null
  li_followers_max: number | null
  li_connections_min: number | null
  li_connections_max: number | null
  ig_followers_min: number | null
  ig_followers_max: number | null
  operator_interaction_min: number | null
  operator_interaction_max: number | null
  set_interaction_min: number | null
  set_interaction_max: number | null
}

/** The modal's list fields and the payload key each one holds. */
const LISTS: Record<string, string> = {
  company_ids: "company_names",
  role_ids: "role_ids",
  education_ids: "education_names",
  investors: "investor_names",
  cities: "cities",
  states: "states",
  countries: "countries",
  metro_areas: "metro_areas",
  macro_regions: "macro_regions",
  company_cities: "company_cities",
  company_states: "company_states",
  company_countries: "company_countries",
  company_metro_areas: "company_metro_areas",
  company_macro_regions: "company_macro_regions",
  yc_batches: "yc_batches",
  entity_types: "entity_types",
  sector_types: "sector_types",
  seniority_bands: "seniority_bands",
  role_tracks: "role_tracks",
  tech_skills: "tech_skills",
  degree_levels: "degree_levels",
  fields_of_study: "fields_of_study",
  company_semantic_queries: "company_semantic_queries",
}

/** The modal's scalar fields and the payload key each one holds. */
const SCALARS: Record<string, string> = {
  funding_stage_min: "funding_stage_min",
  funding_stage_max: "funding_stage_max",
  funding_amount_min: "funding_amount_min",
  funding_amount_max: "funding_amount_max",
  headcount_min: "headcount_min",
  headcount_max: "headcount_max",
  last_funding_before: "last_funding_before",
  last_funding_after: "last_funding_after",
  valuation_min: "valuation_min",
  valuation_max: "valuation_max",
  founded_year_min: "founded_year_min",
  founded_year_max: "founded_year_max",
  graduation_year_min: "graduation_year_min",
  graduation_year_max: "graduation_year_max",
  years_experience_min: "years_experience_min",
  years_experience_max: "years_experience_max",
  age_min: "age_min",
  age_max: "age_max",
  position_after_date: "position_after_date",
  position_before_date: "position_before_date",
  role_semantic_query: "semantic_query",
  x_followers_min: "x_followers_min",
  x_followers_max: "x_followers_max",
  li_followers_min: "li_followers_min",
  li_followers_max: "li_followers_max",
  li_connections_min: "li_connections_min",
  li_connections_max: "li_connections_max",
  ig_followers_min: "ig_followers_min",
  ig_followers_max: "ig_followers_max",
  operator_interaction_min: "operator_interaction_min",
  operator_interaction_max: "operator_interaction_max",
  set_interaction_min: "set_interaction_min",
  set_interaction_max: "set_interaction_max",
}

/** The modal's money fields, in millions on screen and dollars in the payload. */
const MILLIONS = new Set(["funding_amount_min", "funding_amount_max", "valuation_min", "valuation_max"])

const KINDS: Record<string, string> = {
  cities: "city",
  states: "state",
  countries: "country",
  metro_areas: "metro_area",
  macro_regions: "macro_region",
  company_cities: "city",
  company_states: "state",
  company_countries: "country",
  company_metro_areas: "metro_area",
  company_macro_regions: "macro_region",
}

function items(names: string[], type: string, labels?: Record<string, string>): EntityItem[] {
  return names.map((name) => ({ id: name, display_value: labels?.[name] ?? name, type }))
}

function names(value: FilterValue | null | undefined): string[] {
  if (Array.isArray(value)) return value
  return typeof value === "string" && value !== "" ? [value] : []
}

/** The prepared search's filters as the modal edits them. `labels` turns an id into its display name. */
export function toExpanded(
  values: Map<string, FilterValue | null>,
  labels: Record<string, Record<string, string>> = {},
): ExpandedFilters {
  const list = (field: string) =>
    items(names(values.get(LISTS[field] ?? field)), KINDS[field] ?? field, labels[field])
  const text = (field: string) => {
    const value = values.get(SCALARS[field] ?? field)
    return typeof value === "string" ? value : null
  }
  // The modal's money fields read in millions; the payload holds dollars.
  const millions = (field: string) => {
    const value = number(field)
    return value === null ? null : value / 1_000_000
  }
  const number = (field: string) => {
    const value = values.get(SCALARS[field] ?? field)
    return typeof value === "number" ? value : null
  }
  return {
    company_ids: list("company_ids"),
    role_ids: list("role_ids"),
    education_ids: list("education_ids"),
    investors: list("investors"),
    cities: list("cities"),
    states: list("states"),
    countries: list("countries"),
    metro_areas: list("metro_areas"),
    macro_regions: list("macro_regions"),
    company_cities: list("company_cities"),
    company_states: list("company_states"),
    company_countries: list("company_countries"),
    company_metro_areas: list("company_metro_areas"),
    company_macro_regions: list("company_macro_regions"),
    funding_stage_min: text("funding_stage_min"),
    funding_stage_max: text("funding_stage_max"),
    funding_amount_min: millions("funding_amount_min"),
    funding_amount_max: millions("funding_amount_max"),
    headcount_min: number("headcount_min"),
    headcount_max: number("headcount_max"),
    last_funding_before: text("last_funding_before"),
    last_funding_after: text("last_funding_after"),
    valuation_min: millions("valuation_min"),
    valuation_max: millions("valuation_max"),
    founded_year_min: number("founded_year_min"),
    founded_year_max: number("founded_year_max"),
    yc_batches: list("yc_batches"),
    entity_types: list("entity_types"),
    sector_types: list("sector_types"),
    seniority_bands: list("seniority_bands"),
    role_tracks: list("role_tracks"),
    tech_skills: list("tech_skills"),
    degree_levels: list("degree_levels"),
    fields_of_study: list("fields_of_study"),
    graduation_year_min: number("graduation_year_min"),
    graduation_year_max: number("graduation_year_max"),
    years_experience_min: number("years_experience_min"),
    years_experience_max: number("years_experience_max"),
    age_min: number("age_min"),
    age_max: number("age_max"),
    position_after_date: text("position_after_date"),
    position_before_date: text("position_before_date"),
    role_semantic_query: text("role_semantic_query"),
    role_bm25_queries: names(values.get("bm25_queries")),
    company_semantic_queries: list("company_semantic_queries"),
    x_followers_min: number("x_followers_min"),
    x_followers_max: number("x_followers_max"),
    li_followers_min: number("li_followers_min"),
    li_followers_max: number("li_followers_max"),
    li_connections_min: number("li_connections_min"),
    li_connections_max: number("li_connections_max"),
    ig_followers_min: number("ig_followers_min"),
    ig_followers_max: number("ig_followers_max"),
    operator_interaction_min: number("operator_interaction_min"),
    operator_interaction_max: number("operator_interaction_max"),
    set_interaction_min: number("set_interaction_min"),
    set_interaction_max: number("set_interaction_max"),
  }
}

/** The modal's filters written back over the prepared search's filters; keys the modal doesn't hold stay. */
export function fromExpanded(
  filters: ExpandedFilters,
  values: Map<string, FilterValue | null>,
): Map<string, FilterValue | null> {
  const next = new Map(values)
  const record: Record<string, unknown> = { ...filters }
  for (const [field, key] of Object.entries(LISTS)) {
    const list = record[field]
    const names = Array.isArray(list)
      ? list
          .filter((item): item is EntityItem => typeof item === "object" && item !== null)
          .map((item) => item.id)
      : []
    if (names.length > 0 || next.has(key)) next.set(key, names.length > 0 ? names : null)
  }
  for (const [field, key] of Object.entries(SCALARS)) {
    const value = record[field]
    const scalar =
      typeof value === "string" || typeof value === "number" || typeof value === "boolean" ? value : null
    const dollars = MILLIONS.has(field) && typeof scalar === "number" ? scalar * 1_000_000 : scalar
    if (scalar !== null || next.has(key)) next.set(key, dollars)
  }
  const keywords = Array.isArray(filters.role_bm25_queries) ? filters.role_bm25_queries : []
  if (keywords.length > 0 || next.has("bm25_queries"))
    next.set("bm25_queries", keywords.length > 0 ? keywords : null)
  return next
}

// Locations are edited as one list labeled by kind, then split back into their keys.
export function flattenPersonLocations(filters: ExpandedFilters): EntityItem[] {
  return [
    ...filters.cities.map((item) => ({ ...item, type: "city" })),
    ...filters.states.map((item) => ({ ...item, type: "state" })),
    ...filters.countries.map((item) => ({ ...item, type: "country" })),
    ...filters.metro_areas.map((item) => ({ ...item, type: "metro_area" })),
    ...filters.macro_regions.map((item) => ({ ...item, type: "macro_region" })),
  ]
}

export function flattenCompanyLocations(filters: ExpandedFilters): EntityItem[] {
  return [
    ...filters.company_cities.map((item) => ({ ...item, type: "city" })),
    ...filters.company_states.map((item) => ({ ...item, type: "state" })),
    ...filters.company_countries.map((item) => ({ ...item, type: "country" })),
    ...filters.company_metro_areas.map((item) => ({ ...item, type: "metro_area" })),
    ...filters.company_macro_regions.map((item) => ({ ...item, type: "macro_region" })),
  ]
}

export function unflattenPersonLocations(locations: EntityItem[]): Partial<ExpandedFilters> {
  return {
    cities: locations.filter((loc) => loc.type === "city"),
    states: locations.filter((loc) => loc.type === "state"),
    countries: locations.filter((loc) => loc.type === "country"),
    metro_areas: locations.filter((loc) => loc.type === "metro_area"),
    macro_regions: locations.filter((loc) => loc.type === "macro_region"),
  }
}

export function unflattenCompanyLocations(locations: EntityItem[]): Partial<ExpandedFilters> {
  return {
    company_cities: locations.filter((loc) => loc.type === "city"),
    company_states: locations.filter((loc) => loc.type === "state"),
    company_countries: locations.filter((loc) => loc.type === "country"),
    company_metro_areas: locations.filter((loc) => loc.type === "metro_area"),
    company_macro_regions: locations.filter((loc) => loc.type === "macro_region"),
  }
}
