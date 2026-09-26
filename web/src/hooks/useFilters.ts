import { useCallback, useEffect, useState } from "react";

import type { Sort, SortKey } from "@/lib/people/facets";
import { EMPTY, toggled } from "@/lib/sets";
import { readSession, writeSession, type SavedView } from "@/lib/storage";
import { ORDER, type Decision, type Person } from "@/types/people";

export type PeopleView = SavedView;

const DEFAULT_SORT: Sort = { key: "name", dir: 1 };

// Start where the human is needed: the saved tab if it has people, else the first decision that does.
function startView(rows: readonly Person[]): PeopleView {
  const saved = readSession();
  const view: PeopleView = {
    tab: saved?.tab ?? "confirm",
    filters: saved?.filters ?? new Map(),
    text: saved?.text ?? "",
    sort: saved?.sort ?? DEFAULT_SORT,
  };
  if (!rows.some((row) => row.share === view.tab)) {
    view.tab = ORDER.find((decision) => rows.some((row) => row.share === decision)) ?? "confirm";
  }
  return view;
}

/** Tab, facet selections, search text and sort; saved to sessionStorage on every change. */
export function useFilters(rows: readonly Person[]) {
  const [view, setView] = useState<PeopleView>(() => startView(rows));

  useEffect(() => writeSession(view), [view]);

  const setTab = useCallback((tab: Decision) => {
    setView((current) => (current.tab === tab ? current : { ...current, tab }));
  }, []);

  const toggleFilter = useCallback((key: string, value: string) => {
    setView((current) => {
      const filters = new Map(current.filters);
      const held = toggled(filters.get(key) ?? EMPTY, value);
      if (held.size) filters.set(key, held);
      else filters.delete(key);
      return { ...current, filters };
    });
  }, []);

  const setFilters = useCallback((set: Readonly<Record<string, readonly string[]>>) => {
    const filters = new Map(Object.entries(set).map(([key, values]) => [key, new Set(values)]));
    setView((current) => ({ ...current, filters }));
  }, []);

  const setText = useCallback((text: string) => setView((current) => ({ ...current, text })), []);

  // The same column flips direction; a new column starts ascending.
  const setSort = useCallback((key: SortKey) => {
    setView((current) => {
      const dir = current.sort.key === key ? (current.sort.dir === 1 ? -1 : 1) : 1;
      return { ...current, sort: { key, dir } };
    });
  }, []);

  return { view, setTab, toggleFilter, setFilters, setText, setSort };
}
