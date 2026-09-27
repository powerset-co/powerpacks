import type { ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import { plural } from "@/lib/people/copy"
import { isComplete, money, runDate, statusText } from "@/lib/searches/copy"
import type { SearchResult } from "@/types/searches"

interface RunHeaderProps {
  search: SearchResult
  // From the catalog card; a run opened before the catalog loads shows none yet.
  status: string | undefined
  people: number
  actions: ReactNode
}

// The run's name and facts on one compact block; the actions slot sits at its right.
export function RunHeader({ search, status, people, actions }: RunHeaderProps) {
  return (
    <header className="run-head" data-run-head>
      <div className="run-head-id">
        <small>{search.company || "Company unknown"}</small>
        <h1>{search.title}</h1>
        <p className="run-head-facts">
          {status === undefined ? null : (
            <Badge
              variant={isComplete(status) ? "ok" : "muted"}
              className="run-head-status"
              data-status={status}
            >
              {statusText(status)}
            </Badge>
          )}
          <span>{runDate(search.created_at)}</span>
          <span data-people-count>{plural(people, "person")}</span>
          {search.total_cost_usd ? <span>{money(search.total_cost_usd)}</span> : null}
        </p>
      </div>
      {actions ? <div className="run-head-actions">{actions}</div> : null}
    </header>
  )
}
