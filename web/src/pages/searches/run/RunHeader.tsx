import { useRef } from "react"

import { useMoreBelow } from "@/hooks/useMoreBelow"
import { useState, type ReactNode } from "react"

import { DetailsSection } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { plural } from "@/lib/copy"
import { isComplete, money, runDate, statusText } from "@/lib/searches/copy"
import type { SearchResult } from "@/types/searches"

import { TeamPanel } from "./TeamPanel"

interface RunHeaderProps {
  search: SearchResult
  // From the catalog card; a run opened before the catalog loads shows none yet.
  status: string | undefined
  people: number
  actions: ReactNode
}

// The run's name and facts on one compact block, the job description and the hiring
// company's team folded under them (rendering.py _search); the actions slot sits at its right.
export function RunHeader({ search, status, people, actions }: RunHeaderProps) {
  const [jdOpen, setJdOpen] = useState(false)
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
        {search.jd_text ? (
          <DetailsSection
            sectionKey="jd"
            className="run-jd"
            title="Job description"
            open={jdOpen}
            onToggle={setJdOpen}
          >
            <JobDescription text={search.jd_text} />
          </DetailsSection>
        ) : null}
        <TeamPanel search={search} />
      </div>
      {actions ? <div className="run-head-actions">{actions}</div> : null}
    </header>
  )
}

// The description in its own box; a chevron at the foot says there is more below the fold.
function JobDescription({ text }: { text: string }) {
  const box = useRef<HTMLDivElement>(null)
  const more = useMoreBelow(box)
  return (
    <div className="run-jd-box" data-more={more}>
      <div ref={box} className="run-jd-text">
        {text}
      </div>
      <i className="run-jd-more chevron" data-open="true" aria-hidden="true" />
    </div>
  )
}
