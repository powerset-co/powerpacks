import type { SortKey } from "@/lib/people/facets";

// The one row height: the virtualizer spaces rows by it and PeopleShell sets --row-h from it.
export const ROW_H = 36;

export interface Column {
  cls: string;
  label: string;
  sort?: SortKey;
  right?: boolean;
}

// After the select-all box. A column without `sort` is a plain label.
export const COLUMNS: readonly Column[] = [
  { sort: "name", cls: "c-person", label: "Person" },
  { cls: "c-sources", label: "Sources" },
  { sort: "reason", cls: "c-why", label: "Reason" },
  { sort: "relationship", cls: "c-rel", label: "Relationship" },
  { sort: "worth", cls: "c-worth", label: "Worth" },
  { sort: "warmth", cls: "c-warmth", label: "Warmth" },
  { sort: "last", cls: "c-last", label: "Last contact", right: true },
  { sort: "messages", cls: "c-msgs", label: "Interactions", right: true },
];
