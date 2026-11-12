import { useQuery } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "react-router-dom"

import { EmptyState, PageHeader, Toast } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { useSelection } from "@/hooks/useSelection"
import { fetchPeopleIds, fetchPeoplePage } from "@/lib/api/people"
import type { FacetSet, HideKey } from "@/lib/people/facets"
import { LOGBOOK } from "@/lib/people/copy"
import { writeView, type Tab } from "@/lib/people/view"

import { BulkBar, type LogbookAction } from "./BulkBar"
import { PersonDrawer } from "./drawer/PersonDrawer"
import { FilterBar } from "./filters/FilterBar"
import { RefreshSignals } from "./head/RefreshSignals"
import { useDecisions } from "./hooks/useDecisions"
import { useDrawer } from "./hooks/useDrawer"
import { useFilters } from "./hooks/useFilters"
import type { useLogbook } from "./hooks/useLogbook"
import { PEOPLE_QUERY_KEY, usePeopleList } from "./hooks/usePeopleList"
import { usePeopleShortcuts } from "./hooks/usePeopleShortcuts"
import { PeopleLoading } from "./PeopleLoading"
import { PeopleShell } from "./PeopleShell"
import { entriesQuery } from "./logbook/queries"
import { EmptyText } from "./table/EmptyText"
import { PeopleTable, type PeopleTableHandle } from "./table/PeopleTable"

interface PeopleWorkspaceProps {
  // The Logbook reader covers the page: its keys are off.
  reading: boolean
  // The page's one Logbook build (PeoplePage), and opening saved logbooks in the reader.
  logbook: ReturnType<typeof useLogbook>
  onView: (slugs: readonly string[]) => void
}

// The page: the view's list read from the server a slice at a time (its filters, search and sort
// in SQL), with one write for the worth call.
export function PeopleWorkspace({ reading, logbook, onView }: PeopleWorkspaceProps) {
  const [params, setParams] = useSearchParams()
  const initialParams = reading ? new URLSearchParams() : params
  const filters = useFilters(initialParams)
  const { view } = filters
  const list = usePeopleList(view)
  // The saved logbooks fill the Logbook column; while they can't be read the rows still show.
  const catalog = useQuery(entriesQuery)
  const matching = useMemo(() => {
    if (!catalog.data) return list.rows
    const saved = new Map(
      catalog.data.flatMap(({ parent_id, slug }) => (parent_id ? [[parent_id, slug] as const] : [])),
    )
    return list.rows.map((row) => row && { ...row, logbook: saved.get(row.parent_id) ?? "" })
  }, [list.rows, catalog.data])
  const { kinds, totals } = list
  const byId = useMemo(
    () => new Map(matching.flatMap((row) => (row ? [[row.parent_id, row] as const] : []))),
    [matching],
  )
  const { query } = list
  const selection = useSelection(
    list.matching,
    useCallback(() => fetchPeopleIds(query), [query]),
  )
  const drawer = useDrawer(initialParams)
  const { sections } = drawer
  useEffect(() => {
    if (reading) return
    const next = writeView(view, params)
    if (drawer.openId) next.set("person", drawer.openId)
    else next.delete("person")
    next.set("sections", [...sections].join(","))
    if (next.toString() !== params.toString()) setParams(next, { replace: true })
  }, [view, drawer.openId, sections, reading, params, setParams])
  const [focusAt, setFocus] = useState(-1)
  const focus = Math.min(focusAt, matching.length - 1)
  // The open person when their row is not in a loaded slice (a link to them, a later page).
  const openRow = useQuery({
    queryKey: [...PEOPLE_QUERY_KEY, "person", drawer.shownId],
    queryFn: () => fetchPeoplePage(`id=${encodeURIComponent(drawer.shownId ?? "")}&show=linkedin_only`, 0, 1),
    enabled: drawer.shownId !== null && !byId.has(drawer.shownId),
    staleTime: Infinity,
  })
  const table = useRef<PeopleTableHandle>(null)
  const search = useRef<HTMLInputElement>(null)

  const { clear: clearSelection } = selection
  const { openId, refresh, close: closeOpen } = drawer
  const onWritten = useCallback(
    (ids: readonly string[]) => {
      clearSelection()
      if (openId && ids.includes(openId)) refresh()
    },
    [clearSelection, openId, refresh],
  )
  const onDeleted = useCallback(
    (ids: readonly string[]) => {
      clearSelection()
      if (openId && ids.includes(openId)) closeOpen()
    },
    [clearSelection, openId, closeOpen],
  )
  const decisions = useDecisions(byId, onWritten, onDeleted)

  // A decision leaves the drawer on the person it judged and the list where it was: the row
  // takes its new decision in place (or leaves a filtered tab) and nothing scrolls or advances.
  const { close: closeDrawer } = drawer
  const { apply: label } = decisions

  // The bar and the s / p keys act on the selection, else on the open person.
  const targets = selection.selected.size ? [...selection.selected] : openId ? [openId] : []
  // A delete question is about the people it named: a new selection or open person drops it.
  const targetKey = targets.join("\n")
  const { cancelDelete } = decisions
  useEffect(() => cancelDelete(), [targetKey, cancelDelete])
  // The selection's one logbook action: View when everyone has a saved logbook, else Build.
  // The open person's is in the drawer. LinkedIn-only people have no messages to save.
  const messaged = targets.filter((id) => !byId.get(id)?.linkedin_only)
  const saved = messaged.map((id) => byId.get(id)?.logbook ?? "")
  const barLogbook: LogbookAction | null =
    !selection.selected.size || !messaged.length ? null : saved.every(Boolean) ? "view" : "build"
  const barLabel = selection.selected.size
    ? `${selection.selected.size.toLocaleString()} selected`
    : openId
      ? (byId.get(openId)?.name ?? null)
      : null

  // A new tab, filter or search starts over: nothing selected, no focus, back at the top.
  // Each handler keeps one identity, so j/k and the drawer never re-render the bars.
  const { setTab: pickTab, setFilters: pickFilters, toggleHide: pickHide, setText: pickText } = filters
  const startOver = useCallback(() => {
    clearSelection()
    setFocus(-1)
    table.current?.scrollToOffset(0)
  }, [clearSelection])
  // The tab already shown keeps its selection, focus and scroll.
  const setTab = useCallback(
    (tab: Tab) => {
      if (tab === view.tab) return
      pickTab(tab)
      startOver()
    },
    [view.tab, pickTab, startOver],
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
  const toggleHide = useCallback(
    (key: HideKey) => {
      pickHide(key)
      startOver()
    },
    [pickHide, startOver],
  )
  const clearFilters = useCallback(() => setFilters({}), [setFilters])
  const setRelationship = useCallback(
    (kind: string) => setFilters(kind ? { relationship_kind: [kind] } : {}),
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
    label,
    askDelete: decisions.askDelete,
    confirmDelete: decisions.confirmDelete,
    armed: decisions.armed,
    cancelDelete: decisions.cancelDelete,
    targets,
  })

  const shownRow = drawer.shownId ? (byId.get(drawer.shownId) ?? openRow.data?.rows[0] ?? null) : null
  // The search line's right end; it changes with the selection, never with j/k.
  const actions = useMemo(() => {
    // Signals read messages: a selection refreshes its people with messages, and a selection of
    // only LinkedIn-only people has none to refresh. Nothing selected is All.
    const signaled = [...selection.selected].filter((id) => !byId.get(id)?.linkedin_only)
    return (
      <>
        {selection.selected.size && !signaled.length ? null : (
          <RefreshSignals people={signaled} onDone={refresh} />
        )}
      </>
    )
  }, [selection.selected, byId, refresh])
  if (list.loading) return <PeopleLoading />
  if (list.error) return <PeopleShell main={<EmptyState data-empty>{list.error.message}</EmptyState>} />
  return (
    <PeopleShell
      main={
        <>
          <PageHeader title="People" data-head />
          <FilterBar
            ref={search}
            text={view.text}
            tab={view.tab}
            totals={totals}
            relationship={[...(view.filters.get("relationship_kind") ?? [])][0] ?? ""}
            kinds={kinds}
            shown={list.matching}
            onText={setText}
            onTab={setTab}
            onRelationship={setRelationship}
            hide={view.hide}
            onHide={toggleHide}
            actions={actions}
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
            onRange={list.want}
            empty={
              <EmptyText
                hasPeople={totals.all > 0}
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
            section={drawer.section}
            saving={decisions.saving}
            building={logbook.building}
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
            onLogbook={() => (barLogbook === "view" ? onView(saved) : void logbook.build(messaged))}
            onAction={(action) => void label(action, targets)}
            question={decisions.question}
            onDelete={() => decisions.askDelete(targets)}
            onConfirmDelete={decisions.confirmDelete}
            onCancelDelete={decisions.cancelDelete}
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
