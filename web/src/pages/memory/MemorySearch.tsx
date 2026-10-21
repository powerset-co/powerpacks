import { useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { SearchField, VirtualRows } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { searchMemory } from "@/lib/api/memory"
import { cn } from "@/lib/utils"
import type { MemoryPersonHit } from "@/types/memory"

// Measured: a name, a warmth mark and one evidence line.
const ROW_ESTIMATE = 60

// Module level: VirtualRows re-measures every row when its key callback changes identity.
function personKey(person: MemoryPersonHit): string {
  return person.parent
}

/** Warmth 0-4 as the page shows it: one to four marks, none for an unknown relationship. */
function warmthMarks(warmth: number): string {
  return "●".repeat(Math.max(0, Math.min(4, Math.round(warmth))))
}

/** The same search the memory skill runs. Type anything: people come back warmest first with the
 *  claim that matched. A word naming a label (family, wife, coworkers …) lists everyone holding it. */
interface MemorySearchProps {
  onPersonSelect: (parent: string) => void
}

export function MemorySearch({ onPersonSelect }: MemorySearchProps) {
  const [text, setText] = useState("")
  const [query, setQuery] = useState("")
  const answer = useQuery({
    queryKey: ["memory-search", query],
    queryFn: ({ signal }) => searchMemory(query, signal),
    enabled: query !== "",
  })
  const people = answer.data?.people ?? []
  return (
    <section className="flex flex-col gap-3" aria-labelledby="memory-search">
      <h2 id="memory-search" className="m-0 text-[15px] font-semibold">
        Search memory
      </h2>
      <form
        role="search"
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          setQuery(text.trim())
        }}
      >
        <SearchField
          aria-label="Search memory"
          placeholder="family, roblox coworkers, who helped me buy a house…"
          value={text}
          onChange={(event) => setText(event.target.value)}
          className="min-w-0 flex-1"
        />
        <Button type="submit" disabled={!text.trim()}>
          Search
        </Button>
      </form>
      {answer.error ? (
        <p role="alert" className="m-0 text-xs text-bad">
          {errorText(answer.error)}
        </p>
      ) : null}
      {answer.isFetching ? <p className="m-0 text-xs text-muted-foreground">Searching…</p> : null}
      {answer.data && people.length === 0 ? (
        <p className="m-0 text-sm text-muted-foreground">Nothing in memory matches “{query}”.</p>
      ) : null}
      {answer.data && people.length > 0 ? (
        <p className="m-0 text-xs text-muted-foreground tabular-nums">
          {answer.data.total.toLocaleString()} {answer.data.total === 1 ? "person" : "people"}
          {answer.data.layers.length
            ? ` · ${answer.data.filtered ? "within" : "everyone saved as"} ${answer.data.layers.join(", ")}`
            : ""}
          {answer.data.total > people.length ? ` · showing ${people.length}` : ""}
        </p>
      ) : null}
      {people.length > 0 ? (
        <VirtualRows
          role="list"
          aria-label="People"
          className="max-h-[420px] overflow-y-auto border-y border-line"
          items={people}
          rowHeight={ROW_ESTIMATE}
          measure
          getKey={personKey}
          renderRow={(person, index) => (
            <div
              role="listitem"
              className={cn(
                "flex flex-col gap-0.5 py-2 text-[13px]",
                index < people.length - 1 && "border-b border-line",
              )}
            >
              <button
                type="button"
                onClick={() => onPersonSelect(person.parent)}
                className="flex items-baseline justify-between gap-2 text-left font-medium hover:text-primary"
              >
                <span>{person.name}</span>
                <span
                  aria-label={`warmth ${person.warmth.toFixed(1)} of 4`}
                  title={`warmth ${person.warmth.toFixed(1)} of 4`}
                  className="shrink-0 text-[10px] tracking-[2px] text-muted-foreground"
                >
                  {warmthMarks(person.warmth)}
                </span>
              </button>
              {person.evidence[0] ? (
                <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                  {person.evidence[0].claim.text}
                </span>
              ) : null}
            </div>
          )}
        />
      ) : null}
    </section>
  )
}
