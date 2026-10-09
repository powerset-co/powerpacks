import { useQuery } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { Toast } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { useSelection } from "@/hooks/useSelection"
import { nextOpenIndex } from "@/lib/advance"
import type { FacetKey, FacetSet, QuickFilter, TagAction } from "@/lib/people/facets"
import { filterRows } from "@/lib/people/filter"
import { LOGBOOK } from "@/lib/people/copy"
import { withLogbooks } from "@/lib/people/logbook"
import { personKey, type Decision, type Person } from "@/types/people"

import { BulkBar, type LogbookAction } from "./BulkBar"
import { PersonDrawer } from "./drawer/PersonDrawer"
import { FilterBar } from "./filters/FilterBar"
import { QuickFilters } from "./filters/QuickFilters"
import { DecisionTabs } from "./head/DecisionTabs"
import { useDecisions } from "./hooks/useDecisions"
import { useDrawer } from "./hooks/useDrawer"
import { useFilters } from "./hooks/useFilters"
import type { useLogbook } from "./hooks/useLogbook"
import { usePeopleShortcuts } from "./hooks/usePeopleShortcuts"
import { PeopleShell } from "./PeopleShell"
import { entriesQuery } from "./logbook/queries"
import { FacetRail } from "./rail/FacetRail"
import { SetsPanel } from "./sets/SetsPanel"
import { EmptyText } from "./table/EmptyText"
import { PeopleTable, type PeopleTableHandle } from "./table/PeopleTable"
import { ShareUpload } from "./upload/ShareUpload"

interface PeopleWorkspaceProps {
  people: Person[]
  // The Logbook reader covers the page: its keys are off.
  reading: boolean
  // The page's one Logbook build (PeoplePage), and opening saved logbooks in the reader.
  logbook: ReturnType<typeof useLogbook>
  onView: (slugs: readonly string[]) => void
}

// The loaded page: every person once, filtered client-side, with one write for share / private tags.
export function PeopleWorkspace({ people, reading, logbook, onView }: PeopleWorkspaceProps) {
  // The saved logbooks fill the Logbook column; while they can't be read the rows still show.
  const catalog = useQuery(entriesQuery)
  const rows = useMemo(
    () => (catalog.data ? withLogbooks(people, catalog.data) : people),
    [people, catalog.data],
  )
  const filters = useFilters(rows)
  const { view } = filters
  const { matching, counts, quickCounts } = useMemo(() => filterRows(rows, view), [rows, view])
  const byId = useMemo(() => new Map(rows.map((row) => [row.parent_id, row])), [rows])
  const totals = useMemo(() => {
    const counts: Record<Decision, number> = { confirm: 0, yes: 0, no: 0 }
    for (const row of rows) counts[row.share] += 1
    return counts
  }, [rows])
  const selection = useSelection(matching, personKey)
  const drawer = useDrawer()
  const [focusAt, setFocus] = useState(-1)
  const focus = Math.min(focusAt, matching.length - 1)
  const table = useRef<PeopleTableHandle>(null)
  const search = useRef<HTMLInputElement>(null)

  const { clear: clearSelection } = selection
  const { openId, refresh } = drawer
  const onWritten = useCallback(
    (ids: readonly string[]) => {
      clearSelection()
      if (openId && ids.includes(openId)) refresh()
    },
    [clearSelection, openId, refresh],
  )
  const decisions = useDecisions(byId, onWritten)

  // Quick labeling: a label on the open person moves the drawer to whoever is next
  // (lib/advance.ts) once the list reflects the write; nothing cycles.
  const advanceFrom = useRef<{ id: string; index: number } | null>(null)
  const { open: openPerson, close: closeDrawer } = drawer
  const { apply } = decisions
  const label = useCallback(
    async (action: TagAction, targets: readonly string[]) => {
      if (openId && targets.includes(openId)) {
        advanceFrom.current = { id: openId, index: matching.findIndex((row) => row.parent_id === openId) }
      }
      // The list re-renders after a successful write and the effect below consumes this;
      // a skipped or failed write leaves the list alone, so drop it here.
      if (!(await apply(action, targets))) advanceFrom.current = null
    },
    [apply, openId, matching],
  )
  useEffect(() => {
    const from = advanceFrom.current
    if (!from) return
    advanceFrom.current = null
    const next = nextOpenIndex(matching, personKey, from.id, from.index)
    if (next === null) {
      if (!matching.length) closeDrawer()
      return
    }
    const person = matching[next]
    if (!person) return
    setFocus(next)
    openPerson(person.parent_id)
    table.current?.scrollToIndex(next, { align: "auto" })
  }, [matching, openPerson, closeDrawer])

  // The bar and the s / p keys act on the selection, else on the open person.
  const targets = selection.selected.size ? [...selection.selected] : openId ? [openId] : []
  // The selection's one logbook action: View when everyone has a saved logbook, else Build.
  // The open person's is in the drawer.
  const saved = targets.map((id) => byId.get(id)?.logbook ?? "")
  const barLogbook: LogbookAction | null = !selection.selected.size
    ? null
    : saved.every(Boolean)
      ? "view"
      : "build"
  const barLabel = selection.selected.size
    ? `${selection.selected.size.toLocaleString()} selected`
    : openId
      ? (byId.get(openId)?.name ?? null)
      : null

  // A new tab, filter or search starts over: nothing selected, no focus, back at the top.
  // Each handler keeps one identity, so j/k and the drawer never re-render the rail or bars.
  const { setTab: pickTab, toggleFilter: pickFilter, setFilters: pickFilters, setText: pickText } = filters
  const startOver = useCallback(() => {
    clearSelection()
    setFocus(-1)
    table.current?.scrollToOffset(0)
  }, [clearSelection])
  // The tab already shown keeps its selection, focus and scroll.
  const setTab = useCallback(
    (tab: Decision) => {
      if (tab === view.tab) return
      pickTab(tab)
      startOver()
    },
    [view.tab, pickTab, startOver],
  )
  const toggleFilter = useCallback(
    (key: FacetKey, value: string) => {
      pickFilter(key, value)
      startOver()
    },
    [pickFilter, startOver],
  )
  const setFilters = useCallback(
    (set: FacetSet) => {
      pickFilters(set)
      startOver()
    },
    [pickFilters, startOver],
  )
  const setText = useCallback(
    (text: string) => {
      pickText(text)
      startOver()
    },
    [pickText, startOver],
  )
  const clearFilters = useCallback(() => setFilters({}), [setFilters])
  const pickQuick = useCallback(
    (quick: QuickFilter | null) => setFilters(quick ? quick.set : {}),
    [setFilters],
  )

  const { toggle: toggleDrawer } = drawer
  const onOpen = useCallback(
    (id: string, index: number) => {
      setFocus(index)
      toggleDrawer(id)
    },
    [toggleDrawer],
  )

  usePeopleShortcuts({
    active: !reading,
    search,
    table,
    matching,
    focus,
    setFocus,
    setTab,
    selection,
    drawer,
    decisions,
    label,
    targets,
  })

  const shownRow = drawer.shownId ? (byId.get(drawer.shownId) ?? null) : null
  return (
    <PeopleShell
      rail={
        <>
          <SetsPanel />
          <FacetRail filters={view.filters} counts={counts} onValue={toggleFilter} />
        </>
      }
      main={
        <>
          <DecisionTabs
            tab={view.tab}
            totals={totals}
            onTab={setTab}
            action={<ShareUpload onToast={(message) => decisions.showToast({ message })} />}
          />
          <QuickFilters filters={view.filters} counts={quickCounts} onPick={pickQuick} />
          <FilterBar
            ref={search}
            text={view.text}
            filters={view.filters}
            shown={matching.length}
            inTab={totals[view.tab]}
            onText={setText}
            onRemove={toggleFilter}
            onClear={clearFilters}
            notice={
              catalog.error ? (
                <span className="bar-notice" data-logbook-unread>
                  {LOGBOOK.unread}
                  <Button variant="ghost" size="sm" onClick={() => void catalog.refetch()}>
                    {LOGBOOK.retry}
                  </Button>
                </span>
              ) : null
            }
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
            empty={
              <EmptyText
                hasPeople={rows.length > 0}
                filtered={view.filters.size > 0 || view.text !== ""}
                tab={view.tab}
                onClear={clearFilters}
              />
            }
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
            building={logbook.building}
            onAction={(action) => {
              if (openId) void label(action, [openId])
            }}
            onLogbook={() => {
              if (openId) void logbook.build([openId])
            }}
            onView={onView}
            onClose={drawer.close}
            onRetry={refresh}
          />
          <BulkBar
            label={barLabel}
            selection={selection.selected.size > 0}
            disabled={decisions.saving || targets.some((id) => byId.get(id)?.in_progress)}
            building={logbook.building}
            logbook={barLogbook}
            onLogbook={() => (barLogbook === "view" ? onView(saved) : void logbook.build(targets))}
            onAction={(action) => void label(action, targets)}
            onClear={selection.selected.size ? clearSelection : closeDrawer}
          />
          <Toast
            toast={decisions.toast}
            onDismiss={decisions.dismissToast}
            className="toast leading-[1.45]"
          />
        </>
      }
    />
  )
}
