import { pondKept } from "@/lib/searches/ranking"
import type { Pond } from "@/types/searches"

interface PondChainProps {
  ponds: readonly Pond[]
  // The pond the table shows, or null: every pond's people together.
  selected: number | null
  onSelect: (index: number) => void
}

// rendering.py _pond: the search chain top to bottom, each pond's query, how many it kept and
// how many of those no earlier pond found.
export function PondChain({ ponds, selected, onSelect }: PondChainProps) {
  if (!ponds.length) return null
  const seen = new Set<string>()
  const fresh = ponds.map((pond) => {
    const ids = pond.candidates.map((row) => row.person_id)
    const count = ids.filter((id) => !seen.has(id)).length
    ids.forEach((id) => seen.add(id))
    return count
  })
  return (
    <section className="pond-chain" data-ponds aria-label="Search chain">
      <ol>
        {ponds.map((pond, index) => (
          <li key={`${pond.run_id}:${pond.pond_n}`} data-pond={pond.pond_n}>
            <button
              type="button"
              className="pond-card"
              title={[pond.diagnosis || "Final pond", pond.move].filter(Boolean).join(" → ")}
              aria-pressed={selected === index}
              onClick={() => onSelect(index)}
            >
              <span className="pond-n">{pond.pond_n}</span>
              <span className="pond-query">{pond.query}</span>
              <span className="pond-count">
                {index > 0 ? `${(fresh[index] ?? 0).toLocaleString()} new · ` : ""}
                Kept <b>{pondKept(pond).toLocaleString()}</b> of {pond.result_count.toLocaleString()}
              </span>
            </button>
          </li>
        ))}
      </ol>
    </section>
  )
}
