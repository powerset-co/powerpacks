import { useCallback, useMemo, useRef, useState } from "react";

import { Toast } from "@/components/shared";
import { useDecisions } from "@/hooks/useDecisions";
import { useDrawer } from "@/hooks/useDrawer";
import { useFilters } from "@/hooks/useFilters";
import { useSelection } from "@/hooks/useSelection";
import { filterRows } from "@/lib/people/filter";
import { ORDER, type Decision, type Person } from "@/types/people";

import { BulkBar } from "./BulkBar";
import { PersonDrawer } from "./drawer/PersonDrawer";
import { FilterBar } from "./filters/FilterBar";
import { QuickFilters } from "./filters/QuickFilters";
import { DecisionTabs } from "./head/DecisionTabs";
import { PeopleShell } from "./PeopleShell";
import { FacetRail } from "./rail/FacetRail";
import { EmptyText } from "./table/EmptyText";
import { PeopleTable, type PeopleTableHandle } from "./table/PeopleTable";
import { usePeopleShortcuts } from "./usePeopleShortcuts";

// The loaded page: every person once, filtered client-side, with one write for share / private tags.
export function PeopleWorkspace({ rows }: { rows: Person[] }) {
  const filters = useFilters(rows);
  const { view } = filters;
  const { matching, counts, quickCounts } = useMemo(() => filterRows(rows, view), [rows, view]);
  const byId = useMemo(() => new Map(rows.map((row) => [row.parent_id, row])), [rows]);
  const totals = useMemo(
    () => Object.fromEntries(ORDER.map((d) => [d, rows.filter((row) => row.share === d).length])) as Record<Decision, number>,
    [rows],
  );
  const selection = useSelection(matching);
  const drawer = useDrawer();
  const [focusAt, setFocus] = useState(-1);
  const focus = Math.min(focusAt, matching.length - 1);
  const table = useRef<PeopleTableHandle>(null);
  const search = useRef<HTMLInputElement>(null);

  const { clear: clearSelection } = selection;
  const { openId, refresh } = drawer;
  const onWritten = useCallback((ids: readonly string[]) => {
    clearSelection();
    if (openId && ids.includes(openId)) refresh();
  }, [clearSelection, openId, refresh]);
  const decisions = useDecisions(byId, onWritten);

  // A new tab, filter or search starts over: nothing selected, no focus, back at the top.
  const reset = <A extends unknown[]>(change: (...args: A) => void) => (...args: A) => {
    change(...args);
    clearSelection();
    setFocus(-1);
    table.current?.scrollToOffset(0);
  };
  // The tab already shown keeps its selection, focus and scroll.
  const changeTab = reset(filters.setTab);
  const setTab = (tab: Decision) => {
    if (tab !== view.tab) changeTab(tab);
  };
  const toggleFilter = reset(filters.toggleFilter);
  const setFilters = reset(filters.setFilters);
  const setText = reset(filters.setText);
  const clearFilters = () => setFilters({});

  const { toggle: toggleDrawer } = drawer;
  const onOpen = useCallback((id: string, index: number) => {
    setFocus(index);
    toggleDrawer(id);
  }, [toggleDrawer]);

  usePeopleShortcuts({
    search, table, matching, focus, setFocus, setTab, selection, drawer, decisions,
  });

  const shownRow = drawer.shownId ? byId.get(drawer.shownId) ?? null : null;
  return (
    <PeopleShell
      drawerOpen={openId !== null}
      rail={<FacetRail filters={view.filters} counts={counts} onValue={toggleFilter} />}
      main={
        <>
          <DecisionTabs tab={view.tab} totals={totals} total={rows.length} onTab={setTab} />
          <QuickFilters filters={view.filters} counts={quickCounts} onPick={(quick) => setFilters(quick ? quick.set : {})} />
          <FilterBar
            ref={search}
            text={view.text}
            filters={view.filters}
            shown={matching.length}
            inTab={totals[view.tab]}
            onText={setText}
            onRemove={toggleFilter}
            onClear={clearFilters}
          />
          <PeopleTable
            ref={table}
            matching={matching}
            sort={view.sort}
            selected={selection.selected}
            allSelected={selection.allSelected}
            someSelected={selection.someSelected}
            focus={focus}
            openId={openId}
            pending={decisions.pending}
            view={view}
            empty={<EmptyText hasPeople={rows.length > 0} filtered={view.filters.size > 0 || view.text !== ""} tab={view.tab} onClear={clearFilters} />}
            onSort={filters.setSort}
            onSelectAll={selection.toggleAll}
            onSelect={selection.toggle}
            onOpen={onOpen}
          />
        </>
      }
      overlays={
        <>
          <PersonDrawer
            row={shownRow}
            open={openId !== null}
            detail={drawer.detail}
            saving={decisions.saving}
            onAction={(action) => { if (openId) void decisions.apply(action, [openId]); }}
            onClose={drawer.close}
            onRetry={refresh}
          />
          <BulkBar count={selection.selected.size} saving={decisions.saving} onAction={(action) => void decisions.apply(action, [...selection.selected])} onClear={clearSelection} />
          <Toast toast={decisions.toast} onDismiss={decisions.dismissToast} className="toast left-[268px] right-auto leading-[1.45] max-[960px]:left-4" />
        </>
      }
    />
  );
}
