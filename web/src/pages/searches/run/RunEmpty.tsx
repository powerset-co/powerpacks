import { EmptyState, Kbd } from "@/components/shared"

// The main pane before a run is picked.
export function RunEmpty() {
  return (
    <div className="run-empty" data-run-empty>
      <EmptyState>
        <b className="mb-1 block text-[15px] text-foreground">Pick a search</b>
        Choose a search on the left to see who it found. <Kbd>↑</Kbd> <Kbd>↓</Kbd> move, <Kbd>Enter</Kbd>{" "}
        opens, <Kbd>/</Kbd> searches.
      </EmptyState>
    </div>
  )
}
