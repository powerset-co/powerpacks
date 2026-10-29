import type { PersonDetail } from "@/types/people"
// Synthetic people for tests: a columnar payload as GET /api/people/rows returns it.

import { PERSON_COLUMNS, type PeoplePayload, type PersonCell, type PersonColumn } from "@/types/people"

type Cells = Record<PersonColumn, PersonCell>

// Typed as every non-derived `Person` field, so its keys are what PERSON_COLUMNS must list.
export const BASE: Cells = {
  parent_id: "",
  public_identifier: "",
  in_progress: false,
  name: "",
  avatar_url: "",
  title: "",
  company: "",
  location: "",
  channels: [],
  interactions: 0,
  last_interaction: "",
  recency_days: null,
  cadence: "",
  direction: "",
  worth: "",
  worth_source: "",
  relationship_kind: "",
  mode: "",
  hierarchy: "",
  intro_source: "",
  seniority: "",
  function: "",
  warmth: null,
  labels: [],
  flag: "",
  reason: "",
  tags: [],
  is_owner: false,
  linkedin_only: false,
  group_chat_only: false,
  shared_employer: false,
  shared_school: false,
  confidence: null,
}

const PEOPLE: Partial<Cells>[] = [
  {
    parent_id: "p1",
    name: "Jordan Bravo",
    public_identifier: "jordan-bravo",
    title: "Engineer",
    company: "Acme",
    channels: ["gmail", "linkedin"],
    interactions: 40,
    recency_days: 30,
    worth: "no",
    warmth: 2.5,
    relationship_kind: "family",
    reason: "worth_no",
  },
  {
    parent_id: "p2",
    name: "Casey Delta",
    channels: ["imessage"],
    interactions: 12,
    recency_days: 800,
    worth: "no",
    relationship_kind: "close_friend",
    labels: ["is_recruiter"],
    reason: "worth_no",
  },
  {
    parent_id: "p3",
    name: "Riley Echo",
    channels: ["whatsapp"],
    interactions: 3,
    recency_days: 400,
    worth: "no",
    relationship_kind: "family",
    reason: "worth_no",
  },
  {
    parent_id: "p4",
    name: "Morgan Fox",
    location: "Springfield",
    channels: ["gmail"],
    interactions: 90,
    recency_days: 5,
    worth: "yes",
    warmth: 3.9,
    reason: "worth_yes",
  },
]

// Columns in reverse to prove decoding is by name, not position.
const COLUMNS = [...PERSON_COLUMNS].reverse()

export const PAYLOAD: PeoplePayload = {
  counts: { total: 4, upload: 1, private: 3 },
  columns: COLUMNS,
  rows: PEOPLE.map((person) => {
    const cells: Cells = { ...BASE, ...person }
    return COLUMNS.map((column) => cells[column])
  }),
}

/** One drawer detail as GET /api/people/person returns it, for the fixture's first person. */
export const DETAIL: PersonDetail = {
  relationship_kind_source: "machine",
  parent_id: "p1",
  linkedin_url: "",
  headline: "Engineer at Example",
  avatar_url: "",
  worth_reason: "",
  note: "",
  dossier_html: "",
  probabilities: { is_family: 0.9 },
  choice_p: {},
  worth_note: "",
  relationship_to_owner: "sibling",
  events: [{ date: "2024-05", summary: "Moved cities" }],
  shared_context: [],
  topics: ["trips"],
  employers: ["Example"],
  school: "",
  location: "Oakland",
  aliases: [],
  emails: [],
  phones: [],
  interactions: {},
  summary: "",
  experiences: [],
  education: [],
}
