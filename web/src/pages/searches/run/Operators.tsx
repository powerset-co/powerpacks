import { initials } from "@/components/shared"
import type { NetworkOperator } from "@/types/searches"

const SHOWN = 3

// rendering.py .operator-initials: an operator's initials disc (the row's stack, the toolbar's picker).
export function OperatorInitials({ name }: { name: string }) {
  return (
    <span className="operator-initials" aria-hidden="true">
      {initials(name)}
    </span>
  )
}

// rendering.py operator stack: the first three people it came through, then "+N".
export function Operators({ operators }: { operators: readonly NetworkOperator[] }) {
  if (!operators.length) return <span className="result-none">—</span>
  const names = operators.map((operator) => operator.operator_name).join(", ")
  return (
    <span className="operator-stack" title={`Connected via ${names}`}>
      {operators.slice(0, SHOWN).map((operator) => (
        <OperatorInitials key={operator.operator_id} name={operator.operator_name} />
      ))}
      {operators.length > SHOWN ? (
        <span className="operator-initials" aria-hidden="true">
          +{operators.length - SHOWN}
        </span>
      ) : null}
      <span className="sr-only">Connected via {names}</span>
    </span>
  )
}
