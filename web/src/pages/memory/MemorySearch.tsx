import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { SearchField, VirtualRows } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { memoryPeople, searchMemory } from "@/lib/api/memory"
import { cn } from "@/lib/utils"
import type { MemoryOrigin, MemoryPerson, MemorySelector } from "@/types/memory"

const ORIGIN: Record<MemoryOrigin, string> = {
  source_name: "name",
  human_tag: "your tag",
  topic: "topic",
  saved_value: "saved fact",
}

// Measured: a row is a name plus evidence of any length.
const ROW_ESTIMATE = 56

// Module level: VirtualRows re-measures every row when its key callback changes identity.
function personKey(person: MemoryPerson): string {
  return person.parent
}

/** Search the directory: the meanings a phrase resolves to, each with its count, then everyone
 *  under the chosen one. An exact match lists the first meaning's people until another is picked;
 *  suggestions list nobody until one is. */
export function MemorySearch() {
  const [text, setText] = useState("")
  const [query, setQuery] = useState("")
  const [chosen, setChosen] = useState<MemorySelector | null>(null)
  const meanings = useQuery({
    queryKey: ["memory-search", query],
    queryFn: ({ signal }) => searchMemory(query, signal),
    enabled: query !== "",
  })
  const first = meanings.data?.[0]
  const selector = chosen ?? (first?.match === "exact or confirmed alias" ? first.selector : null)
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
          setChosen(null)
          setQuery(text.trim())
        }}
      >
        <SearchField
          aria-label="Search memory"
          placeholder="investor, climate, met at YC…"
          value={text}
          onChange={(event) => setText(event.target.value)}
          className="min-w-0 flex-1"
        />
        <Button type="submit" disabled={!text.trim()}>
          Search
        </Button>
      </form>
      {meanings.error ? (
        <p role="alert" className="m-0 text-xs text-bad">
          {errorText(meanings.error)}
        </p>
      ) : null}
      {meanings.isFetching ? <p className="m-0 text-xs text-muted-foreground">Searching…</p> : null}
      {meanings.data?.length === 0 ? (
        <p className="m-0 text-sm text-muted-foreground">Nothing in memory matches “{query}”.</p>
      ) : null}
      {meanings.data?.length ? (
        <ul className="m-0 flex list-none flex-wrap gap-2 p-0" aria-label="Meanings">
          {meanings.data.map((meaning) => {
            const selected =
              selector?.origin === meaning.selector.origin && selector.value === meaning.selector.value
            return (
              <li key={`${meaning.selector.origin}:${meaning.selector.value}`}>
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() => setChosen(meaning.selector)}
                  className={cn(
                    "flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors duration-fast ease-out motion-reduce:transition-none",
                    selected
                      ? "border-primary bg-surface-2 text-foreground"
                      : "border-border bg-transparent text-muted-foreground hover:bg-secondary hover:text-foreground",
                  )}
                >
                  <span className="font-semibold">{meaning.selector.value}</span>{" "}
                  <span>{ORIGIN[meaning.selector.origin]}</span>{" "}
                  <span className="tabular-nums">· {meaning.count.toLocaleString()}</span>
                </button>
              </li>
            )
          })}
        </ul>
      ) : null}
      {!selector && meanings.data?.length ? (
        <p className="m-0 text-xs text-muted-foreground">
          No exact match: pick the meaning you want to list.
        </p>
      ) : null}
      {selector ? <People key={`${selector.origin}:${selector.value}`} selector={selector} /> : null}
    </section>
  )
}

/** Everyone under one meaning, fifty at a time. */
function People({ selector }: { selector: MemorySelector }) {
  const people = useInfiniteQuery({
    queryKey: ["memory-people", selector],
    queryFn: ({ pageParam, signal }) => memoryPeople(selector, pageParam, signal),
    initialPageParam: "",
    getNextPageParam: (page) => page.next_after || undefined,
  })
  if (people.error)
    return (
      <p role="alert" className="m-0 text-xs text-bad">
        {errorText(people.error)}
      </p>
    )
  if (!people.data) return null
  const rows = people.data.pages.flatMap((page) => page.people)
  return (
    <div className="flex flex-col gap-2">
      <p className="m-0 text-xs text-muted-foreground tabular-nums">
        {rows.length.toLocaleString()} of {people.data.pages[0]?.total.toLocaleString()} people
      </p>
      <VirtualRows
        role="list"
        aria-label="People"
        className="max-h-[360px] overflow-y-auto border-y border-line"
        items={rows}
        rowHeight={ROW_ESTIMATE}
        measure
        getKey={personKey}
        renderRow={(person, index) => (
          <div
            role="listitem"
            className={cn(
              "flex flex-col gap-0.5 py-2 text-[13px]",
              index < rows.length - 1 && "border-b border-line",
            )}
          >
            <span className="font-medium">{person.name}</span>
            {person.evidence[0]?.quote ? (
              <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                {person.evidence[0].quote}
              </span>
            ) : null}
          </div>
        )}
      />
      {people.hasNextPage ? (
        <Button
          type="button"
          className="self-start"
          onClick={() => void people.fetchNextPage()}
          disabled={people.isFetchingNextPage}
        >
          {people.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      ) : null}
    </div>
  )
}
