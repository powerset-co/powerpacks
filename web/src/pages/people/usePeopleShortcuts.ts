import type { RefObject } from "react";

import type { useDecisions } from "@/hooks/useDecisions";
import type { useDrawer } from "@/hooks/useDrawer";
import { useKeyboard } from "@/hooks/useKeyboard";
import type { useSelection } from "@/hooks/useSelection";
import { ORDER, type Decision, type Person } from "@/types/people";

import type { PeopleTableHandle } from "./table/PeopleTable";

interface ShortcutTargets {
  search: RefObject<HTMLInputElement>;
  table: RefObject<PeopleTableHandle>;
  matching: readonly Person[];
  focus: number;
  setFocus: (index: number) => void;
  setTab: (tab: Decision) => void;
  selection: ReturnType<typeof useSelection>;
  drawer: ReturnType<typeof useDrawer>;
  decisions: ReturnType<typeof useDecisions>;
}

// The People page's keys wired to its state; the Keyboard shortcuts list in the rail names them.
export function usePeopleShortcuts({ search, table, matching, focus, setFocus, setTab, selection, drawer, decisions }: ShortcutTargets) {
  const focused = matching[focus];
  const act = (action: "share" | "private" | "worth") => {
    if (selection.selected.size) void decisions.apply(action, [...selection.selected]);
  };

  useKeyboard({
    focusSearch: () => {
      search.current?.focus();
      search.current?.select();
    },
    focusFacets: () => document.querySelector<HTMLButtonElement>("[data-rail] [data-facet-value]")?.focus(),
    switchTab: (position) => setTab(ORDER[position] ?? "confirm"),
    move: (step) => {
      if (!matching.length) return;
      const next = Math.min(matching.length - 1, Math.max(0, focus + step));
      setFocus(next);
      table.current?.scrollToIndex(next, { align: "auto" });
    },
    toggleFocused: () => {
      if (!focused) return false;
      selection.toggle(focused.parent_id);
      return true;
    },
    selectAll: selection.toggleAll,
    share: () => act("share"),
    keepPrivate: () => act("private"),
    useWorth: () => act("worth"),
    undo: () => void decisions.undo(),
    openFocused: () => {
      if (focused) drawer.toggle(focused.parent_id);
    },
    escape: () => {
      if (drawer.openId) drawer.close();
      else if (selection.selected.size) selection.clear();
    },
  });
}
