// Synthetic people for tests: a columnar payload as GET /api/people/rows returns it.

import type { PeoplePayload, PersonCell, PersonColumn } from "@/types/people";

type Cells = Record<PersonColumn, PersonCell>;

const BASE: Cells = {
  parent_id: "", public_identifier: "", name: "", has_avatar: false, title: "", company: "", location: "",
  channels: [], interactions: 0, last_interaction: "", recency_days: null, cadence: "", direction: "",
  worth: "", worth_source: "", relationship_kind: "", mode: "", hierarchy: "", intro_source: "",
  seniority: "", function: "", warmth: null, labels: [], flag: "", share: "confirm", reason: "",
  share_source: "machine", tags: [], is_owner: false, linkedin_only: false, group_chat_only: false,
  shared_employer: false, shared_school: false, confidence: null,
};

const PEOPLE: Partial<Cells>[] = [
  { parent_id: "p1", name: "Jordan Bravo", public_identifier: "jordan-bravo", title: "Engineer", company: "Acme",
    channels: ["gmail", "linkedin"], interactions: 40, recency_days: 30, worth: "yes", warmth: 2.5,
    relationship_kind: "family", share: "confirm", reason: "family" },
  { parent_id: "p2", name: "Casey Delta", channels: ["imessage"], interactions: 12, recency_days: 800,
    worth: "maybe", relationship_kind: "close_friend", labels: ["is_recruiter"], share: "confirm",
    reason: "worth_maybe" },
  { parent_id: "p3", name: "Riley Echo", channels: ["whatsapp"], interactions: 3, recency_days: 400,
    relationship_kind: "family", share: "confirm", reason: "family", tags: ["private"] },
  { parent_id: "p4", name: "Morgan Fox", location: "Springfield", channels: ["gmail"], interactions: 90,
    recency_days: 5, worth: "yes", warmth: 3.9, share: "yes", reason: "worth_yes" },
];

// Columns in reverse to prove decoding is by name, not position.
const COLUMNS = (Object.keys(BASE) as PersonColumn[]).reverse();

export const PAYLOAD: PeoplePayload = {
  counts: { total: 4, upload: 1, confirm: 3, private: 0 },
  columns: COLUMNS,
  rows: PEOPLE.map((person) => {
    const cells: Cells = { ...BASE, ...person };
    return COLUMNS.map((column) => cells[column]);
  }),
};
