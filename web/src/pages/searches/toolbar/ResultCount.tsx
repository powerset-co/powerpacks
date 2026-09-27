import { CountRoll } from "@/components/shared"
import { countText } from "@/lib/searches/filters"

interface ResultCountProps {
  shown: number
  total: number
}

// "12 of 125 results": the numbers roll; a screen reader hears the settled text once.
export function ResultCount({ shown, total }: ResultCountProps) {
  return (
    <span
      className="whitespace-nowrap text-xs font-semibold tabular-nums text-muted-foreground"
      data-result-count
    >
      <span className="sr-only" role="status">
        {countText(shown, total)}
      </span>
      <span aria-hidden="true">
        <CountRoll value={shown} />
        {shown === total ? null : (
          <>
            {" of "}
            <CountRoll value={total} />
          </>
        )}
        {total === 1 ? " result" : " results"}
      </span>
    </span>
  )
}
