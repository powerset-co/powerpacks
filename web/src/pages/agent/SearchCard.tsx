import { Button } from "@/components/ui/button"
import type { PreparedSearch } from "@/lib/agent/preparedSearch"
import { send } from "@/lib/agent/store"

interface SearchCardProps {
  search: PreparedSearch
  // Execute and Modify: the newest card's, while the agent waits for an answer.
  actions: boolean
}

/**
 * A prepared search in the chat: its query, filters, pool and notes, and the run-or-change choice.
 * A blocked one shows what blocks it and can only be modified.
 */
export function SearchCard({ search, actions }: SearchCardProps) {
  return (
    <section
      aria-label="Search"
      className="rise-in flex flex-col gap-3 rounded-[var(--radius-m)] border border-line bg-card p-4 shadow-[var(--shadow-1)]"
    >
      <h2 className="m-0 text-[15px] font-semibold">{search.query}</h2>
      {search.filters.length > 0 && (
        <ul aria-label="Filters" className="m-0 flex list-none flex-wrap gap-1.5 p-0">
          {search.filters.map((filter) => (
            <li
              key={filter.label}
              className="rounded-full border border-line px-2.5 py-1 text-[11.5px] font-semibold leading-snug"
            >
              <span className="text-faint">{filter.label}:</span> {filter.value}
            </li>
          ))}
        </ul>
      )}
      {search.pool && (
        <p className="m-0 text-xs text-muted-foreground">
          Pool: {search.pool.matched.toLocaleString()} of {search.pool.total.toLocaleString()} people
        </p>
      )}
      {search.notes.length > 0 && (
        <ul
          aria-label="Notes"
          className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-muted-foreground"
        >
          {search.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {search.blockers.length > 0 && (
        <ul aria-label="Blockers" className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-warn">
          {search.blockers.map((blocker) => (
            <li key={blocker}>{blocker}</li>
          ))}
        </ul>
      )}
      {actions && (
        <div className="flex flex-wrap gap-2">
          {search.blockers.length === 0 && (
            <Button variant="primary" onClick={() => void send("execute")}>
              Execute
            </Button>
          )}
          <Button onClick={() => document.getElementById("agent-message")?.focus()}>Modify</Button>
        </div>
      )}
    </section>
  )
}
