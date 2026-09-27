import { EmptyState, Kbd } from "@/components/shared"

// The main pane before a run is picked, with the page's keys: the arrows drive the list,
// j/k the open run's people (hooks/useSidebarKeys, hooks/useResultKeys).
export function RunEmpty() {
  return (
    <div className="run-empty" data-run-empty>
      <EmptyState>
        <b className="mb-1 block text-[15px] text-foreground">Pick a search</b>
        Choose a search on the left to see who it found. <Kbd>↑</Kbd> <Kbd>↓</Kbd> move, <Kbd>Enter</Kbd>{" "}
        opens, <Kbd>/</Kbd> searches.
        <span className="mt-2 block" data-result-keys>
          In a search: <Kbd>J</Kbd> <Kbd>K</Kbd> move, <Kbd>Enter</Kbd> opens a person, <Kbd>T</Kbd> tags,{" "}
          <Kbd>S</Kbd> scores.
        </span>
      </EmptyState>
    </div>
  )
}
