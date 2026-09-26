// The People API's shapes, field for field with packs/ingestion/primitives/share/web/model.py.

export type Decision = "confirm" | "yes" | "no";

/** The decision tabs, in page order: where the human is needed first. */
export const ORDER: readonly Decision[] = ["confirm", "yes", "no"];

/** The worth call; "" when no one judged it. */
export type Worth = "yes" | "maybe" | "no" | "";

/** Who made a call (`worth_source`, `share_source`); "" when no one did. */
export type DecidedBy = "human" | "machine" | "";

/** One list row per parent (`SharePerson`), plus the fields the page derives on load. */
export interface Person {
  parent_id: string;
  public_identifier: string;
  name: string;
  has_avatar: boolean;
  title: string;
  company: string;
  location: string;
  channels: string[];
  interactions: number;
  last_interaction: string;
  recency_days: number | null;
  cadence: string;
  direction: string;
  worth: Worth;
  worth_source: DecidedBy;
  relationship_kind: string;
  mode: string;
  hierarchy: string;
  intro_source: string;
  seniority: string;
  function: string;
  warmth: number | null;
  labels: string[];
  flag: string;
  share: Decision;
  reason: string;
  share_source: DecidedBy;
  tags: string[];
  is_owner: boolean;
  linkedin_only: boolean;
  group_chat_only: boolean;
  shared_employer: boolean;
  shared_school: boolean;
  confidence: number | null;
  // Derived on load.
  last: string;
  warmthBucket: string;
  search: string;
}

/** A person's identity everywhere on the page: selection, pending writes, virtual row keys. */
export const personKey = (row: Person): string => row.parent_id;

/** The server's columns: every `Person` field but the derived ones. */
export type PersonColumn = Exclude<keyof Person, "last" | "warmthBucket" | "search">;

/** The client's copy of model.py `PEOPLE_COLUMNS`, in its order. tests/test_share_web.py pins
 *  the two lists equal, so a server field rename fails a test instead of blanking a column. */
export const PERSON_COLUMNS = [
  "parent_id", "public_identifier", "name", "has_avatar", "title", "company", "location", "channels",
  "interactions", "last_interaction", "recency_days", "cadence", "direction", "worth", "worth_source",
  "relationship_kind", "mode", "hierarchy", "intro_source", "seniority", "function", "warmth", "labels",
  "flag", "share", "reason", "share_source", "tags", "is_owner", "linkedin_only", "group_chat_only",
  "shared_employer", "shared_school", "confidence",
] as const satisfies readonly PersonColumn[];

export type PersonCell = Person[PersonColumn];

export interface ShareCounts {
  total: number;
  upload: number;
  confirm: number;
  private: number;
}

/** GET /api/people/rows: the column names once, one array per person. */
export interface PeoplePayload {
  counts: ShareCounts;
  columns: string[];
  rows: PersonCell[][];
}

/** One dated line of the relationship. */
export interface FactEvent {
  date: string;
  summary: string;
}

/** GET /api/people/person: what only the drawer shows. */
export interface PersonDetail {
  parent_id: string;
  linkedin_url: string;
  headline: string;
  avatar_url: string;
  worth_reason: string;
  note: string;
  dossier_html: string;
  probabilities: Record<string, number>;
  choice_p: Record<string, number>;
  worth_note: string;
  relationship_to_owner: string;
  events: FactEvent[];
  shared_context: string[];
  topics: string[];
  employers: string[];
  school: string;
  location: string;
  aliases: string[];
  emails: string[];
  phones: string[];
}

/** POST /api/people/tags entry: the tags one parent should hold (absolute set). */
export interface TagChange {
  parent_id: string;
  tags: string[];
}

/** POST /api/people/tags result row: the re-decided share for one parent. */
export interface TagResult {
  parent_id: string;
  share: Decision;
  reason: string;
  share_source: DecidedBy;
  tags: string[];
}
