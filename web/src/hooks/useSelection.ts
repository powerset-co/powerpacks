import { useCallback, useMemo, useState } from "react";

import { EMPTY, toggled } from "@/lib/sets";
import type { Person } from "@/types/people";

/** Selected parent ids; select-all works on every matching person, not just the mounted rows. */
export function useSelection(matching: readonly Person[]) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(EMPTY);

  const toggle = useCallback((id: string) => {
    setSelected((current) => toggled(current, id));
  }, []);

  const clear = useCallback(() => setSelected(EMPTY), []);

  const selectedHere = useMemo(
    () => matching.filter((row) => selected.has(row.parent_id)).length,
    [matching, selected],
  );
  const allSelected = matching.length > 0 && selectedHere === matching.length;

  // All matching selected already: clear; otherwise add every matching person.
  const toggleAll = useCallback(() => {
    setSelected((current) => {
      if (matching.every((row) => current.has(row.parent_id))) return EMPTY;
      const next = new Set(current);
      for (const row of matching) next.add(row.parent_id);
      return next;
    });
  }, [matching]);

  return { selected, toggle, toggleAll, clear, allSelected, someSelected: selectedHere > 0 && !allSelected };
}
