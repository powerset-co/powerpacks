import { useVirtualizer } from "@tanstack/react-virtual"
import { useRef, useState, type ReactNode } from "react"

import { cn } from "@/lib/utils"

const ROW_HEIGHT = 36
const OVERSCAN = 20
// A short table lays out as one; a long one scrolls inside a fixed height with only the rows in
// view mounted, so a hundred-row reply (or a thousand) costs nothing to draw.
const SCROLL_FROM_ROWS = 20
const MAX_HEIGHT = "24rem"

/** A table of text cells, as a chat reply shows one: the row and column count and a Copy
 *  (tab-separated, pastes into a sheet) above a sticky header; cells rendered by `cell` so a
 *  Markdown table keeps its links and bold. The same component for any table the app draws
 *  from data: head and rows, nothing else to learn. */
export function DataTable({
  head,
  rows,
  cell = (text) => text,
  className,
}: {
  head: readonly string[]
  rows: readonly (readonly string[])[]
  cell?: (text: string) => ReactNode
  className?: string
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  const long = rows.length > SCROLL_FROM_ROWS
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
    enabled: long,
  })
  const items = long ? virtualizer.getVirtualItems() : rows.map((_, index) => ({ index, start: 0, end: 0 }))
  const above = long && items.length > 0 ? (items[0]?.start ?? 0) : 0
  const below =
    long && items.length > 0 ? virtualizer.getTotalSize() - (items[items.length - 1]?.end ?? 0) : 0
  const copy = () => {
    const text = [head, ...rows]
      .map((row) => row.map((value) => value.replace(/\t|\n/g, " ")).join("\t"))
      .join("\n")
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div className={cn("overflow-hidden rounded-[var(--radius-s)] border border-line", className)}>
      <div className="flex items-center justify-between border-b border-line bg-card px-3 py-1.5 text-[11px] text-muted-foreground">
        <span>
          {rows.length.toLocaleString()} {rows.length === 1 ? "row" : "rows"} · {head.length}{" "}
          {head.length === 1 ? "column" : "columns"}
        </span>
        <button
          type="button"
          onClick={copy}
          className="cursor-pointer border-0 bg-transparent p-0 text-[11px] text-muted-foreground hover:text-foreground"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div ref={scroller} className="overflow-auto" style={long ? { maxHeight: MAX_HEIGHT } : undefined}>
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 z-10 bg-card">
            <tr>
              {head.map((text, index) => (
                <th
                  key={`${index}:${text}`}
                  className="whitespace-nowrap border-b border-line px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-[.06em] text-faint"
                >
                  {cell(text)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {above > 0 ? (
              <tr aria-hidden>
                <td style={{ height: above, padding: 0 }} colSpan={head.length} />
              </tr>
            ) : null}
            {items.map(({ index }) => {
              const row = rows[index] ?? []
              return (
                <tr
                  key={index}
                  data-index={index}
                  ref={long ? virtualizer.measureElement : undefined}
                  className="border-b border-line last:border-b-0"
                >
                  {row.map((text, column) => (
                    <td key={`${column}:${text}`} className="px-3 py-2 align-top tabular-nums">
                      {cell(text)}
                    </td>
                  ))}
                </tr>
              )
            })}
            {below > 0 ? (
              <tr aria-hidden>
                <td style={{ height: below, padding: 0 }} colSpan={head.length} />
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  )
}
