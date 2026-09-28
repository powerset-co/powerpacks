import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { EmptyState } from "@/components/shared"
import { useListEntrance } from "@/hooks/useListEntrance"
import { useReducedMotion } from "@/hooks/useReducedMotion"
import {
  groupByRecency,
  matches,
  readCatalogFilter,
  writeCatalogFilter,
  type CatalogFilter,
} from "@/lib/searches/catalog"
import type { SearchCard } from "@/types/searches"

import { useSidebarKeys } from "../hooks/useSidebarKeys"
import { CatalogSearch } from "./CatalogSearch"
import { RunItem } from "./RunItem"

interface CatalogListProps {
  cards: readonly SearchCard[]
  selectedId: string | null
  onOpen: (runId: string) => void
}

// The loaded catalog: the search box and the runs grouped by recency.
export function CatalogList({ cards, selectedId, onOpen }: CatalogListProps) {
  const [filter, setFilter] = useState<CatalogFilter>(readCatalogFilter)
  const [highlight, setHighlight] = useState<string | null>(null)
  const search = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const reduced = useReducedMotion()

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
    // Enter on the open run itself belongs to the results (hooks/useResultKeys).
    open: () => {
      const card = visible[at]
      if (card && card.run_id !== selectedId) open(card.run_id)
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
        <CatalogSearch
          ref={search}
          filter={filter}
          onChange={(next) => {
            setFilter(next)
            writeCatalogFilter(next)
            setHighlight(null)
          }}
        />
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
