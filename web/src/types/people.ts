// The People API's shapes, field for field with packs/ingestion/primitives/share/web/model.py.

// The derived buckets the client computes and filters on (lib/people/facets.ts).
export const LAST = ["< 1 year", "1–2 years", "> 2 years", "Never"] as const
export type LastBucket = (typeof LAST)[number]
export const WARMTH = ["Distant (0–1)", "Friendly (1–2)", "Close (2–3)", "Inner circle (3–4)"] as const
export type WarmthBucket = (typeof WARMTH)[number]

/** The worth call; "" when no one judged it. */
export type Worth = "yes" | "maybe" | "no" | ""

/** The Worth select's values: everyone, each worth call, or no call yet. */
export type WorthTab = "all" | "yes" | "maybe" | "no" | "unjudged"
export const WORTH_TABS: readonly WorthTab[] = ["all", "yes", "maybe", "no", "unjudged"]

/** The tab a person's worth puts them under. */
export const worthTab = (worth: Worth): Exclude<WorthTab, "all"> => worth || "unjudged"

/** Who made the worth call; "" when no one has (model.py `_worth_source`). */
export type DecidedBy = "human" | "machine"

/** One list row per parent (`SharePerson`), plus the fields the page derives on load. */
export interface Person {
  parent_id: string
  public_identifier: string
  in_progress: boolean
  name: string
  avatar_url: string
  title: string
  company: string
  location: string
  channels: string[]
  interactions: number
  last_interaction: string
  recency_days: number | null
  cadence: string
  direction: string
  worth: Worth
  worth_source: DecidedBy | ""
  relationship_kind: string
  mode: string
  hierarchy: string
  intro_source: string
  seniority: string
  function: string
  warmth: number | null
  labels: string[]
  flag: string
  reason: string
  tags: string[]
  is_owner: boolean
  linkedin_only: boolean
  group_chat_only: boolean
  shared_employer: boolean
  shared_school: boolean
  confidence: number | null
  // Derived on load.
  last: LastBucket
  warmthBucket: WarmthBucket | ""
  search: string
  // The saved Logbook entry's slug, "" for none; null until the saved logbooks are read.
  logbook: string | null
}

/** A person's identity everywhere on the page: selection, pending writes, virtual row keys. */
export const personKey = (row: Person): string => row.parent_id

/** The server's columns: every `Person` field but the derived ones. */
export type PersonColumn = Exclude<keyof Person, "last" | "warmthBucket" | "search" | "logbook">

/** The client's copy of model.py `PEOPLE_COLUMNS`, in its order. tests/test_share_web.py pins
 *  the two lists equal, so a server field rename fails a test instead of blanking a column. */
export const PERSON_COLUMNS = [
  "parent_id",
  "public_identifier",
  "in_progress",
  "name",
  "avatar_url",
  "title",
  "company",
  "location",
  "channels",
  "interactions",
  "last_interaction",
  "recency_days",
  "cadence",
  "direction",
  "worth",
  "worth_source",
  "relationship_kind",
  "mode",
  "hierarchy",
  "intro_source",
  "seniority",
  "function",
  "warmth",
  "labels",
  "flag",
  "reason",
  "tags",
  "is_owner",
  "linkedin_only",
  "group_chat_only",
  "shared_employer",
  "shared_school",
  "confidence",
] as const satisfies readonly PersonColumn[]

export type PersonCell = Person[PersonColumn]

export interface ShareCounts {
  total: number
  upload: number
  private: number
}

/** GET /api/people/rows: the column names once, one array per person. */
export interface PeoplePayload {
  counts: ShareCounts
  columns: string[]
  rows: PersonCell[][]
}

/** One dated line of the relationship. */
export interface FactEvent {
  date: string
  summary: string
}

/** GET /api/people/person: what only the drawer shows. */
export interface PersonDetail {
  relationship_kind_source: "human" | "machine" | ""
  parent_id: string
  linkedin_url: string
  headline: string
  avatar_url: string
  worth_reason: string
  note: string
  dossier_html: string
  probabilities: Record<string, number>
  choice_p: Record<string, number>
  worth_note: string
  relationship_to_owner: string
  events: FactEvent[]
  shared_context: string[]
  topics: string[]
  employers: string[]
  school: string
  location: string
  aliases: string[]
  emails: string[]
  phones: string[]
  /** Source family -> emails or messages, as the importer counted them. */
  interactions: Record<string, number>
}

/** POST /api/people/worth result row: one parent after the human's worth call. */
export interface WorthResult {
  parent_id: string
  worth: Worth
  worth_source: DecidedBy | ""
  reason: string
  tags: string[]
}
