// Every filter a person can set on a prepared search, grouped the way the cloud app's Edit
// Filters modal groups them, with the control each one is edited through. The keys are the
// expand payload's `role_search_filters` keys; the skill writes them back under the same names.

import type { FilterValue } from "./preparedSearch"

/** The seniority bands the index knows (`packs/search/primitives/shared/seniority_bands.py`). */
export const SENIORITY_BANDS = [
  "owner",
  "partner",
  "c-suite",
  "vice-president",
  "director",
  "principal",
  "staff",
  "manager",
  "senior",
  "mid",
  "junior",
  "entry",
  "trainee",
]

export const DEGREE_LEVELS = ["Bachelors", "Masters", "MBA", "PhD", "MD", "JD"]

export const FUNDING_STAGES = [
  "pre_seed",
  "seed",
  "series_a",
  "series_b",
  "series_c",
  "series_d",
  "series_e",
  "series_f",
  "series_g",
  "series_h",
  "ipo",
  "exited",
]

export const TEMPORALS = ["all", "current", "past"]

export type Control =
  /** A list of names: chips with inline edit and remove, and an add field (or a picker). */
  | { kind: "chips"; key: string; options?: string[]; readonly?: boolean }
  /** Several lists shown as one row, each chip labeled by its kind: the person's locations. */
  | { kind: "kinds"; keys: Record<string, string> }
  /** One line of text. */
  | { kind: "text"; key: string }
  /** A min and a max. */
  | { kind: "pair"; keys: [string, string]; type: "number" | "date" }
  /** A min and a max from a vocabulary. */
  | { kind: "select-pair"; keys: [string, string]; options: string[] }
  /** A yes/no. */
  | { kind: "toggle"; key: string }

export interface Field {
  group: string
  label: string
  control: Control
}

const LOCATION_KINDS = {
  metro_areas: "metro",
  cities: "city",
  states: "state",
  countries: "country",
  macro_regions: "region",
}

const COMPANY_LOCATION_KINDS = {
  company_metro_areas: "metro",
  company_cities: "city",
  company_states: "state",
  company_countries: "country",
  company_macro_regions: "region",
}

export const GROUPS = ["Role", "Location", "Company", "Education", "Social", "Interactions", "Other"]

export const FIELDS: Field[] = [
  {
    group: "Role",
    label: "Seniority",
    control: { kind: "chips", key: "seniority_bands", options: SENIORITY_BANDS },
  },
  { group: "Role", label: "Keywords", control: { kind: "chips", key: "bm25_queries" } },
  { group: "Role", label: "Role query", control: { kind: "text", key: "semantic_query" } },
  { group: "Role", label: "Role ids", control: { kind: "chips", key: "role_ids", readonly: true } },
  { group: "Role", label: "Tracks", control: { kind: "chips", key: "role_tracks", readonly: true } },
  {
    group: "Role",
    label: "Years",
    control: { kind: "pair", keys: ["years_experience_min", "years_experience_max"], type: "number" },
  },
  { group: "Role", label: "Age", control: { kind: "pair", keys: ["age_min", "age_max"], type: "number" } },
  {
    group: "Role",
    label: "Position dates",
    control: { kind: "pair", keys: ["position_after_date", "position_before_date"], type: "date" },
  },
  { group: "Role", label: "Current role only", control: { kind: "toggle", key: "is_current_role" } },
  { group: "Role", label: "Current company only", control: { kind: "toggle", key: "is_current_company" } },
  { group: "Location", label: "Location", control: { kind: "kinds", keys: LOCATION_KINDS } },
  { group: "Company", label: "Companies", control: { kind: "chips", key: "company_names" } },
  { group: "Company", label: "Company ids", control: { kind: "chips", key: "company_ids", readonly: true } },
  {
    group: "Company",
    label: "Company query",
    control: { kind: "chips", key: "company_semantic_queries", readonly: true },
  },
  { group: "Company", label: "Investors", control: { kind: "chips", key: "investor_names" } },
  { group: "Company", label: "Company location", control: { kind: "kinds", keys: COMPANY_LOCATION_KINDS } },
  { group: "Company", label: "Sector", control: { kind: "chips", key: "sector_types" } },
  { group: "Company", label: "Entity type", control: { kind: "chips", key: "entity_types" } },
  { group: "Company", label: "Technology", control: { kind: "chips", key: "technology_types" } },
  { group: "Company", label: "Customers", control: { kind: "chips", key: "customer_types" } },
  {
    group: "Company",
    label: "Stage",
    control: {
      kind: "select-pair",
      keys: ["funding_stage_min", "funding_stage_max"],
      options: FUNDING_STAGES,
    },
  },
  {
    group: "Company",
    label: "Funding ($M)",
    control: { kind: "pair", keys: ["funding_amount_min", "funding_amount_max"], type: "number" },
  },
  {
    group: "Company",
    label: "Headcount",
    control: { kind: "pair", keys: ["headcount_min", "headcount_max"], type: "number" },
  },
  {
    group: "Company",
    label: "Valuation ($M)",
    control: { kind: "pair", keys: ["valuation_min", "valuation_max"], type: "number" },
  },
  {
    group: "Company",
    label: "Founded",
    control: { kind: "pair", keys: ["founded_year_min", "founded_year_max"], type: "number" },
  },
  {
    group: "Company",
    label: "Last funding",
    control: { kind: "pair", keys: ["last_funding_after", "last_funding_before"], type: "date" },
  },
  { group: "Company", label: "Stages", control: { kind: "chips", key: "stages" } },
  { group: "Company", label: "Company stages", control: { kind: "chips", key: "company_stages" } },
  { group: "Company", label: "Stage name", control: { kind: "text", key: "stage" } },
  { group: "Company", label: "YC batches", control: { kind: "chips", key: "yc_batches" } },
  { group: "Company", label: "Accelerators", control: { kind: "chips", key: "accelerators" } },
  { group: "Education", label: "Schools", control: { kind: "chips", key: "education_names" } },
  {
    group: "Education",
    label: "School ids",
    control: { kind: "chips", key: "education_ids", readonly: true },
  },
  { group: "Education", label: "Fields of study", control: { kind: "chips", key: "fields_of_study" } },
  {
    group: "Education",
    label: "Degree",
    control: { kind: "chips", key: "degree_levels", options: DEGREE_LEVELS },
  },
  {
    group: "Education",
    label: "Graduation year",
    control: { kind: "pair", keys: ["graduation_year_min", "graduation_year_max"], type: "number" },
  },
  {
    group: "Social",
    label: "X followers",
    control: { kind: "pair", keys: ["x_followers_min", "x_followers_max"], type: "number" },
  },
  {
    group: "Social",
    label: "LinkedIn followers",
    control: { kind: "pair", keys: ["li_followers_min", "li_followers_max"], type: "number" },
  },
  {
    group: "Social",
    label: "LinkedIn connections",
    control: { kind: "pair", keys: ["li_connections_min", "li_connections_max"], type: "number" },
  },
  {
    group: "Social",
    label: "Instagram followers",
    control: { kind: "pair", keys: ["ig_followers_min", "ig_followers_max"], type: "number" },
  },
  {
    group: "Interactions",
    label: "With you",
    control: { kind: "pair", keys: ["operator_interaction_min", "operator_interaction_max"], type: "number" },
  },
  {
    group: "Interactions",
    label: "With the set",
    control: { kind: "pair", keys: ["set_interaction_min", "set_interaction_max"], type: "number" },
  },
  { group: "Other", label: "Tech skills", control: { kind: "chips", key: "tech_skills" } },
]

/** The payload keys a field edits. */
export function fieldKeys(field: Field): string[] {
  const control = field.control
  switch (control.kind) {
    case "kinds":
      return Object.keys(control.keys)
    case "pair":
    case "select-pair":
      return [...control.keys]
    case "chips":
    case "text":
    case "toggle":
      return [control.key]
  }
}

/** A row under Other for a key the table doesn't name, with the control its value's shape needs. */
export function fieldFor(key: string, value: FilterValue | null | undefined): Field {
  const words = key.replaceAll("_", " ")
  const label = `${words.charAt(0).toUpperCase()}${words.slice(1)}`
  if (Array.isArray(value)) return { group: "Other", label, control: { kind: "chips", key } }
  if (typeof value === "boolean") return { group: "Other", label, control: { kind: "toggle", key } }
  return { group: "Other", label, control: { kind: "text", key } }
}

/** True when any of the field's keys holds a value in `values`. */
export function fieldHasValue(field: Field, values: Map<string, FilterValue | null>): boolean {
  return fieldKeys(field).some((key) => {
    const value = values.get(key)
    return value !== null && value !== undefined && !(Array.isArray(value) && value.length === 0)
  })
}
