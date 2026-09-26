// The People page's filters survive a reload within the tab (sessionStorage), as in people.js.

import { FACET_BY_KEY, type Sort } from "@/lib/people/facets";
import { ORDER, type Decision } from "@/types/people";

const FILTERS_KEY = "powerpacks:people-filters:v2";

export interface SavedView {
  tab: Decision;
  filters: ReadonlyMap<string, ReadonlySet<string>>;
  text: string;
  sort: Sort;
}

interface StoredView {
  tab?: string;
  filters?: Record<string, string[]>;
  text?: string;
  sort?: Sort;
}

/** The saved view, unknown facets and tabs dropped; null when nothing usable is stored. */
export function readSession(): Partial<SavedView> | null {
  let saved: StoredView | null = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(FILTERS_KEY) ?? "null") as StoredView | null;
  } catch {
    return null;
  }
  if (!saved?.filters) return null;
  const view: Partial<SavedView> = {
    filters: new Map(Object.entries(saved.filters).filter(([key]) => FACET_BY_KEY.has(key))
      .map(([key, values]) => [key, new Set(values)])),
    text: saved.text ?? "",
  };
  if (saved.sort) view.sort = saved.sort;
  const tab = ORDER.find((decision) => decision === saved?.tab);
  if (tab) view.tab = tab;
  return view;
}

export function writeSession({ tab, filters, text, sort }: SavedView): void {
  const stored = Object.fromEntries([...filters].map(([key, values]) => [key, [...values]]));
  try {
    sessionStorage.setItem(FILTERS_KEY, JSON.stringify({ tab, filters: stored, text, sort }));
  } catch {
    // Storage blocked: the view just won't survive a reload.
  }
}
