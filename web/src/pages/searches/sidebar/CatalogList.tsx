import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { EmptyState } from "@/components/shared"
import { useListEntrance } from "@/hooks/useListEntrance"
import { useReducedMotion } from "@/hooks/useReducedMotion"
import type { SearchCard } from "@/types/searches"

import { useSidebarKeys } from "../hooks/useSidebarKeys"
import {
  companiesOf,
  groupByRecency,
  initialFilter,
  matches,
  statusesOf,
  versionsOf,
  type CatalogFilter,
} from "../lib/catalog"
import { CatalogFilters } from "./CatalogFilters"
import { RunItem } from "./RunItem"

interface CatalogListProps {
  cards: readonly SearchCard[]
  selectedId: string | null
  onOpen: (runId: string) => void
}

// The loaded catalog: filters, the count, and the runs grouped by recency.
export function CatalogList({ cards, selectedId, onOpen }: CatalogListProps) {
  const [filter, setFilter] = useState<CatalogFilter>(() => initialFilter(cards))
  const [highlight, setHighlight] = useState<string | null>(null)
  const search = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const reduced = useReducedMotion()

  const options = useMemo(
    () => ({ versions: versionsOf(cards), companies: companiesOf(cards), statuses: statusesOf(cards) }),
    [cards],
  )
  const visible = useMemo(() => cards.filter((card) => matches(card, filter)), [cards, filter])
  const groups = useMemo(() => groupByRecency(visible, new Date()), [visible])
  useListEntrance(() => list.current, visible, visible.length, ".run-group-title, .run-item")

  // The highlight starts on the open run and never points at a hidden one.
  const cursor = highlight ?? selectedId
  const at = visible.findIndex((card) => card.run_id === cursor)

  const open = useCallback(
    (runId: string) => {
      setHighlight(runId)
      onOpen(runId)
    },
    [onOpen],
  )

  useSidebarKeys({
    focusSearch: () => search.current?.focus(),
    move: (step) => {
      const next = visible[Math.min(visible.length - 1, Math.max(0, at + step))]
      if (next) setHighlight(next.run_id)
    },
    open: () => {
      const card = visible[at]
      if (card) open(card.run_id)
    },
  })

  useEffect(() => {
    if (!highlight) return
    const rows = list.current?.querySelectorAll<HTMLElement>(".run-item") ?? []
    ;[...rows]
      .find((row) => row.dataset.runId === highlight)
      ?.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" })
  }, [highlight, reduced])

  return (
    <>
      <div className="rail-top">
        <CatalogFilters
          ref={search}
          filter={filter}
          {...options}
          onChange={(next) => {
            setFilter(next)
            setHighlight(null)
          }}
        />
        <p className="list-count" data-catalog-count aria-live="polite">
          {visible.length.toLocaleString()} of {cards.length.toLocaleString()} searches
        </p>
      </div>
      <div ref={list} className="run-list" data-run-list>
        {groups.map((group) => (
          <section key={group.title} className="run-group" aria-label={group.title}>
            <h3 className="run-group-title">{group.title}</h3>
            {group.cards.map((card) => (
              <RunItem
                key={card.run_id}
                card={card}
                selected={card.run_id === selectedId}
                highlighted={highlight !== null && card.run_id === highlight}
                onOpen={open}
              />
            ))}
          </section>
        ))}
        {visible.length ? null : (
          <EmptyState className="my-6 px-3 text-[12.5px]" data-catalog-empty>
            No searches match
          </EmptyState>
        )}
      </div>
    </>
  )
}
