import { initials } from "@/components/shared"
import { operatorDetail } from "@/lib/searches/sources"
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

interface ConnectedViaProps {
  operators: readonly NetworkOperator[]
  // The line under a name: every source and count by default, or one source's share.
  detail?: (operator: NetworkOperator) => string
  // The Gmail accounts under each name.
  accounts?: boolean
}

// rendering.py .network-operator (the drawer, a source's popover): each person it came
// through, their sources and counts, and the Gmail accounts.
export function ConnectedVia({ operators, detail = operatorDetail, accounts = true }: ConnectedViaProps) {
  if (!operators.length) return null
  return (
    <section className="review-sections">
      <h4>Connected via</h4>
      <ul className="evidence-operators" data-operators>
        {operators.map((operator) => {
          const line = detail(operator)
          return (
            <li key={operator.operator_id}>
              <OperatorInitials name={operator.operator_name} />
              <span>
                <b>{operator.operator_name}</b>
                {line ? <small>{line}</small> : null}
                {accounts
                  ? operator.gmail_account_details.map((account) => (
                      <small key={account.email} className="evidence-account">
                        {account.email} · {account.interactions.toLocaleString("en-US")} emails
                      </small>
                    ))
                  : null}
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
