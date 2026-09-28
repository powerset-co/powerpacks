import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { createPortal } from "react-dom"

import { EmptyState, VirtualRows, type VirtualRowsHandle } from "@/components/shared"
import { useListEntrance } from "@/hooks/useListEntrance"
import { nextOpenIndex } from "@/lib/advance"
import { buildScoreFeedback, yourScore } from "@/lib/searches/feedback"
import type { ResultRow as Result } from "@/lib/searches/ranking"
import { rubricChoices } from "@/lib/searches/rubric"
import { isPinned, PIN_TAG } from "@/lib/searches/tags"
import type { FeedbackRecord } from "@/types/searches"

import { send } from "../dialogs/send"
import { useResultKeys } from "../hooks/useResultKeys"
import { ResultDrawer } from "./ResultDrawer"
import { ResultRow } from "./ResultRow"
import { ReviewBar } from "./ReviewBar"
import type { RowContext } from "./RowActions"
import { ROW_ESTIMATE } from "./rows"

export type ResultItem =
  { kind: "heading"; key: string; text: string } | { kind: "row"; key: string; result: Result }

interface ResultsTableProps {
  items: readonly ResultItem[]
  ranked: boolean
  labels: boolean
  empty: ReactNode
  rowContext: RowContext
}

// Which row the drawer shows; `key` stays set while it animates shut.
interface DrawerState {
  key: string | null
  open: boolean
}

const SHUT: DrawerState = { key: null, open: false }
const itemKey = (item: ResultItem) => item.key
const resultKey = (result: Result) => result.key

/**
 * The virtualized people table and the review drawer over it. A row click or Enter opens the
 * row in the drawer (the same row again closes it); j/k move the focused row and the open
 * drawer follows. A rubric score from the bar or its digit, or a save in the score dialog,
 * moves the drawer to the next row (lib/advance.ts), and past the last row closes it.
 */
export function ResultsTable({ items, ranked, labels, empty, rowContext }: ResultsTableProps) {
  const [focus, setFocus] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<DrawerState>(SHUT)
  const rows = useRef<VirtualRowsHandle>(null)
  const results = useMemo(() => items.flatMap((item) => (item.kind === "row" ? [item.result] : [])), [items])
  // Rows rise in when the list changes (a run, a pond, a filter), not when a row's tags or score do.
  const signature = items.map(itemKey).join("\n")
  const list = useMemo(() => ({ signature }), [signature])
  useListEntrance(() => rows.current?.element ?? null, list, results.length, ".results-heading, .result-row")

  // A filter that drops the open row closes the drawer.
  const shown = results.find((result) => result.key === drawer.key) ?? null
  const open = drawer.open && shown !== null
  const current = open ? shown : null

  const scrollTo = useCallback(
    (key: string) => {
      rows.current?.scrollToIndex(
        items.findIndex((item) => item.key === key),
        { align: "auto" },
      )
    },
    [items],
  )
  const toggle = useCallback((key: string) => {
    setFocus(key)
    setDrawer((now) => ({ key, open: !(now.open && now.key === key) }))
  }, [])
  const close = () => setDrawer((now) => ({ ...now, open: false }))
  const show = (key: string) => {
    setFocus(key)
    setDrawer({ key, open: true })
    scrollTo(key)
  }

  // The open candidate was scored: open the next row, or close past the last.
  const advance = (done: Result) => {
    const next = results[nextOpenIndex(results, resultKey, done.key, results.indexOf(done)) ?? -1]
    if (next) show(next.key)
    else close()
  }
  // The score dialog's save reaches the table through the rows' shared context; the latest
  // render's rule decides whether it advances, so the context keeps one identity.
  const afterSave = useRef<(record: FeedbackRecord) => void>(() => undefined)
  useEffect(() => {
    afterSave.current = (record) => {
      if (current?.candidate?.person_id === record.person_id) advance(current)
    }
  })
  const context = useMemo<RowContext>(
    () => ({
      ...rowContext,
      onSaved: (record) => {
        rowContext.onSaved(record)
        afterSave.current(record)
      },
    }),
    [rowContext],
  )

  const choices = useMemo(() => rubricChoices(rowContext.rubric), [rowContext.rubric])
  const candidate = current?.candidate
  const rate = (score: number) => {
    if (!candidate) return false
    const record = buildScoreFeedback(rowContext.runId, candidate, score, "")
    context.onSaved(record)
    send(rowContext, record)
    return true
  }
  const pin = () => {
    if (!candidate) return false
    rowContext.tags.toggle(candidate.person_id, PIN_TAG)
    return true
  }

  // t and s press the focused row's own tag and score buttons, so their panels open at the row.
  const focused = items.findIndex((item) => item.key === focus)
  const press = (action: "tag" | "score") => {
    const key = items[focused]?.key
    if (key === undefined) return false
    const row = [...(rows.current?.element?.querySelectorAll<HTMLElement>("[data-result-key]") ?? [])].find(
      (element) => element.dataset.resultKey === key,
    )
    row?.querySelector<HTMLElement>(`[data-row-action="${action}"]`)?.click()
    return true
  }

  useResultKeys({
    move: (step) => {
      const at = results.findIndex((result) => result.key === focus)
      const next = results[at < 0 ? 0 : Math.min(results.length - 1, Math.max(0, at + step))]
      if (!next) return
      if (open) show(next.key)
      else {
        setFocus(next.key)
        scrollTo(next.key)
      }
    },
    toggle: () => {
      const key = items[focused]?.key
      if (key !== undefined) toggle(key)
      return key !== undefined
    },
    tag: () => press("tag"),
    score: () => press("score"),
    pin,
    rate: (digit) => {
      const choice = choices.find((option) => String(option.score) === digit)
      return choice !== undefined && rate(choice.score)
    },
    close: () => {
      if (open) close()
      return open
    },
  })

  const score = current ? (yourScore(current, rowContext.queued)?.score ?? null) : null
  return (
    <section className="results" data-results aria-label="People">
      <div className="result-head" aria-hidden="true">
        <span>Candidate</span>
        <span>{ranked ? "Overall score and reasoning" : "Trait scores and reasoning"}</span>
      </div>
      <VirtualRows
        handle={rows}
        className="results-viewport"
        data-results-viewport
        items={items}
        rowHeight={ROW_ESTIMATE}
        measure
        getKey={itemKey}
        renderRow={(item) =>
          item.kind === "heading" ? (
            <h3 className="results-heading">{item.text}</h3>
          ) : (
            <ResultRow
              result={item.result}
              ranked={ranked}
              labels={labels}
              open={open && item.key === drawer.key}
              focused={item.key === focus}
              context={context}
              onToggle={toggle}
            />
          )
        }
      />
      {results.length ? null : (
        <EmptyState className="results-empty" data-results-empty>
          {empty}
        </EmptyState>
      )}
      {/* On the body: the run pane is a size container, which would hold a fixed panel inside it. */}
      {createPortal(
        <>
          <ResultDrawer
            result={shown}
            open={open}
            ranked={ranked}
            labels={labels}
            context={context}
            onClose={close}
          />
          <ReviewBar
            label={current?.row.name ?? null}
            choices={choices}
            score={score}
            scorable={candidate !== undefined}
            tagsLoading={rowContext.tags.loading}
            pinned={candidate ? isPinned(rowContext.tags.tagged, candidate.person_id) : false}
            onScore={rate}
            onTag={() => press("tag")}
            onPin={pin}
            onClose={close}
          />
        </>,
        document.body,
      )}
    </section>
  )
}
