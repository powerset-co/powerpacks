import { CountRoll } from "@/components/shared"
import { countText } from "@/lib/searches/filters"

interface ResultCountProps {
  shown: number
  total: number
}

// "12 of 125 results": the numbers roll; a screen reader hears the settled text once. The shown
// number rolls only between two filtered counts: when " of 125" appears or goes, it starts at
// its value, so the line never reads "125 of 125" on its way to "12 of 125".
export function ResultCount({ shown, total }: ResultCountProps) {
  const filtered = shown !== total
  return (
    <span
      className="whitespace-nowrap text-xs font-semibold tabular-nums text-muted-foreground"
      data-result-count
    >
      <span className="sr-only" role="status">
        {countText(shown, total)}
      </span>
      <span aria-hidden="true">
        <CountRoll key={filtered ? "filtered" : "all"} value={shown} />
        {filtered ? (
          <>
            {" of "}
            <CountRoll value={total} />
          </>
        ) : null}
        {total === 1 ? " result" : " results"}
      </span>
    </span>
  )
}
