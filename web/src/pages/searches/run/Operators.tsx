import { initials } from "@/components/shared"
import type { NetworkOperator } from "@/types/searches"

const SHOWN = 3

// rendering.py operator stack: the first three people it came through, then "+N".
export function Operators({ operators }: { operators: readonly NetworkOperator[] }) {
  if (!operators.length) return <span className="result-none">—</span>
  const names = operators.map((operator) => operator.operator_name)
  return (
    <span className="operator-stack" title={`Connected via ${names.join(", ")}`}>
      {operators.slice(0, SHOWN).map((operator) => (
        <span key={operator.operator_id} className="operator-initials">
          {initials(operator.operator_name)}
        </span>
      ))}
      {operators.length > SHOWN ? (
        <span className="operator-initials">+{operators.length - SHOWN}</span>
      ) : null}
      <span className="sr-only">Connected via {names.join(", ")}</span>
    </span>
  )
}
