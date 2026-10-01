import { useState } from "react"

import { SHOW_FEWER, showMore } from "@/lib/review/copy"
import { foldFacts } from "@/lib/review/person"

interface FactListProps {
  items: readonly string[]
}

// The `fact_list` macro: a Work / Education list showing three entries, the rest behind
// "+ show N more" / "show fewer".
export function FactList({ items }: FactListProps) {
  const [expanded, setExpanded] = useState(false)
  const { shown, rest } = foldFacts(items)
  return (
    <>
      <ul className="fact-list">
        {shown.map((item, position) => (
          <li key={position}>{item}</li>
        ))}
        {rest.map((item, position) => (
          <li key={shown.length + position} hidden={!expanded}>
            {item}
          </li>
        ))}
      </ul>
      {rest.length ? (
        <button type="button" className="show-more" onClick={() => setExpanded(!expanded)}>
          {expanded ? SHOW_FEWER : showMore(rest.length)}
        </button>
      ) : null}
    </>
  )
}
