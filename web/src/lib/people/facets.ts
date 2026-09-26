// Facets, quick filters, the filter/count pass, sorting and the tag toggle, ported 1:1 from people.js.
// Facets: OR within one, AND across, always within the selected decision tab.

import type { Person } from "@/types/people";

import { label, sentence, type TextKind } from "./copy";

const YEAR = 365;

export const LAST = ["< 1 year", "1–2 years", "> 2 years", "Never"] as const;
export const WARMTH = ["Distant (0–1)", "Friendly (1–2)", "Close (2–3)", "Inner circle (3–4)"] as const;

export function lastBucket(days: number | null): string {
  if (days === null) return LAST[3];
  if (days < YEAR) return LAST[0];
  if (days <= 2 * YEAR) return LAST[1];
  return LAST[2];
}

export function warmthBucket(value: number | null): string {
  if (value === null) return "";
  return WARMTH[Math.min(3, Math.floor(value))] ?? "";
}

/** `get` returns the row's values for the facet; `words` names the TEXT map that words its values. */
export interface FacetDef {
  key: string;
  label: string;
  get: (row: Person) => string[];
  order?: readonly string[];
  words?: TextKind;
  more?: boolean;
  search?: boolean;
}

const one = (value: string): string[] => (value ? [value] : []);

// The default facets stay open; the `more` ones collapse under "More filters".
export const FACETS: readonly FacetDef[] = [
  { key: "reason", label: "Reason", get: (r) => [r.reason], words: "reason" },
  { key: "worth", label: "Worth", get: (r) => [r.worth || "unjudged"], order: ["yes", "maybe", "no", "unjudged"], words: "worth" },
  { key: "relationship_kind", label: "Relationship", get: (r) => one(r.relationship_kind) },
  { key: "last", label: "Last contact", get: (r) => [r.last], order: LAST },
  { key: "channels", label: "Sources", get: (r) => r.channels, order: ["gmail", "imessage", "whatsapp", "linkedin"] },
  { key: "linkedin", label: "LinkedIn", get: (r) => [r.public_identifier ? "Has LinkedIn" : "No LinkedIn"],
    order: ["Has LinkedIn", "No LinkedIn"] },
  { key: "worth_source", label: "Worth decided by", get: (r) => one(r.worth_source), words: "source", more: true },
  { key: "tags", label: "Your tags", get: (r) => r.tags, more: true },
  { key: "labels", label: "Relationship labels", get: (r) => r.labels, words: "labels", more: true, search: true },
  { key: "function", label: "Function", get: (r) => one(r.function), words: "function", more: true },
  { key: "seniority", label: "Seniority", get: (r) => one(r.seniority), words: "seniority", more: true },
  { key: "mode", label: "Conversation", get: (r) => one(r.mode), words: "mode", more: true },
  { key: "warmth", label: "Warmth", get: (r) => one(r.warmthBucket), order: WARMTH, more: true },
  { key: "direction", label: "Who writes", get: (r) => one(r.direction), words: "direction", more: true },
  { key: "hierarchy", label: "Reporting relationship", get: (r) => one(r.hierarchy), words: "hierarchy", more: true },
  { key: "intro_source", label: "How you met", get: (r) => one(r.intro_source), words: "intro_source", more: true },
  { key: "evidence", label: "Evidence", get: (r) => [
    ...(r.linkedin_only ? ["No relationship labels"] : []), ...(r.group_chat_only ? ["Group chats only"] : []),
    ...(r.shared_employer ? ["Shared employer"] : []), ...(r.shared_school ? ["Shared school"] : []),
  ], more: true },
];

export const FACET_BY_KEY: ReadonlyMap<string, FacetDef> = new Map(FACETS.map((facet) => [facet.key, facet]));

export function facetText(facet: FacetDef, value: string): string {
  return facet.words ? label(facet.words, value) : sentence(value);
}

/** Quick filters: named facet selections, counted within the tab. */
export interface QuickFilter {
  name: string;
  set: Readonly<Record<string, readonly string[]>>;
}

export const QUICK: readonly QuickFilter[] = [
  { name: "Family", set: { relationship_kind: ["family"] } },
  { name: "Sensitive context", set: { labels: ["sensitive_context"] } },
  { name: "Service providers", set: { relationship_kind: ["service_provider"] } },
  { name: "Recruiters", set: { labels: ["is_recruiter"] } },
  { name: "Strangers", set: { labels: ["is_stranger"] } },
  { name: "Automated senders", set: { labels: ["is_automated_sender"] } },
  { name: "Last contact > 2 years", set: { last: [LAST[2]] } },
  { name: "Close friends", set: { relationship_kind: ["close_friend"] } },
];

export type SortKey = "name" | "reason" | "relationship" | "worth" | "warmth" | "last" | "messages";

export interface Sort {
  key: SortKey;
  dir: 1 | -1;
}

const WORTH_ORDER = ["yes", "maybe", "no", ""];

const SORTERS: Readonly<Record<SortKey, (a: Person, b: Person) => number>> = {
  name: (a, b) => a.name.localeCompare(b.name),
  reason: (a, b) => a.reason.localeCompare(b.reason) || a.name.localeCompare(b.name),
  relationship: (a, b) => (a.relationship_kind || "~").localeCompare(b.relationship_kind || "~"),
  worth: (a, b) => WORTH_ORDER.indexOf(a.worth) - WORTH_ORDER.indexOf(b.worth),
  warmth: (a, b) => (b.warmth ?? -1) - (a.warmth ?? -1),
  last: (a, b) => (a.recency_days ?? 1e9) - (b.recency_days ?? 1e9),
  messages: (a, b) => b.interactions - a.interactions,
};

export function sortRows(rows: readonly Person[], { key, dir }: Sort): Person[] {
  return [...rows].sort((a, b) => dir * SORTERS[key](a, b));
}

export type TagAction = "share" | "private" | "worth";

/** The tags a person holds after a share / keep-private / use-worth action. */
export function nextTags(row: Person, action: TagAction): string[] {
  const tags = new Set(row.tags);
  if (action === "share") { tags.delete("private"); tags.add("share"); }
  if (action === "private") { tags.delete("share"); tags.add("private"); }
  if (action === "worth") { tags.delete("share"); tags.delete("private"); }
  return [...tags].sort();
}
