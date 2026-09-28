import { pondKept } from "@/lib/searches/ranking"
import type { Pond } from "@/types/searches"

interface PondChainProps {
  ponds: readonly Pond[]
  // Without a screen the table shows one pond at a time: the cards pick it.
  selected: number | null
  onSelect: (index: number) => void
}

// rendering.py _pond: the search chain top to bottom, each pond's query and how many it kept.
export function PondChain({ ponds, selected, onSelect }: PondChainProps) {
  if (!ponds.length) return null
  return (
    <section className="pond-chain" data-ponds aria-label="Search chain">
      <ol>
        {ponds.map((pond, index) => {
          const body = (
            <>
              <span className="pond-n">{pond.pond_n}</span>
              <span className="pond-query">{pond.query}</span>
              <span className="pond-count">
                Kept <b>{pondKept(pond).toLocaleString()}</b> of {pond.result_count.toLocaleString()}
              </span>
            </>
          )
          const note = [pond.diagnosis || "Final pond", pond.move].filter(Boolean).join(" → ")
          return (
            <li key={`${pond.run_id}:${pond.pond_n}`} data-pond={pond.pond_n}>
              {selected === null ? (
                <div className="pond-card" title={note}>
                  {body}
                </div>
              ) : (
                <button
                  type="button"
                  className="pond-card"
                  title={note}
                  aria-pressed={selected === index}
                  onClick={() => onSelect(index)}
                >
                  {body}
                </button>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
