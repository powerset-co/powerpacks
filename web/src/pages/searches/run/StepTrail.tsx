import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef } from "react"

import { useAgent } from "@/lib/agent/store"
import { cn } from "@/lib/utils"
import type { SearchResult, SearchStep } from "@/types/searches"

import { searchRunKey } from "../hooks/useSearchRun"
import { goBack, isFilter, stepLabel, stepTrail } from "./steps"

interface StepTrailProps {
  search: SearchResult
  // Null where there is no chat to run it on (the browser's Searches page): the tags are plain.
  onGoBack: ((step: SearchStep) => void) | null
  disabled: boolean
}

/**
 * How the results got here: the search, then each refinement up to the one showing, `+` between.
 * An earlier tag goes back to its step; the ✕ on the last goes back to the one before it. A filter
 * step is dashed and reads "only …". A run without steps shows none.
 */
export function StepTrail({ search, onGoBack, disabled }: StepTrailProps) {
  const trail = stepTrail(search)
  if (trail.length === 0) return null
  const byId = new Map(trail.map((step) => [step.id, step]))
  return (
    <nav className="run-trail" aria-label="Refinements" data-step-trail>
      {trail.map((step, index) => {
        const label = stepLabel(search.title, step)
        const className = cn("run-trail-tag", step.criteria !== null && "added", isFilter(step) && "only")
        const last = index === trail.length - 1
        const parent = step.parent === null ? undefined : byId.get(step.parent)
        return (
          <span key={step.id} className="run-trail-item">
            {index > 0 ? <span className="run-trail-plus">+</span> : null}
            {last || onGoBack === null ? (
              <span className={className} data-step-tag>
                {label}
                {last && parent && onGoBack ? (
                  <button
                    type="button"
                    className="run-trail-undo"
                    aria-label={`Go back to ${stepLabel(search.title, parent)}`}
                    disabled={disabled}
                    onClick={() => onGoBack(parent)}
                  >
                    ✕
                  </button>
                ) : null}
              </span>
            ) : (
              <button
                type="button"
                className={cn(className, "link")}
                title="Go back to this"
                disabled={disabled}
                data-step-tag
                onClick={() => onGoBack(step)}
              >
                {label}
              </button>
            )}
          </span>
        )
      })}
    </nav>
  )
}

const RERANK = "search_network_pipeline.py rerank"

/**
 * The trail beside a chat (the desktop app): going back runs on the chat, and both wait while the
 * run or a turn is running. The run is read again when a rerank command finishes and when a turn
 * ends; the catalog's poll can't tell one finished step from the next.
 */
export function ChatStepTrail({ search, status }: { search: SearchResult; status: string | undefined }) {
  const agent = useAgent()
  const client = useQueryClient()
  const runId = search.run_id
  const reranked = agent.entries.filter(
    (entry) => entry.kind === "command" && entry.command.includes(RERANK) && entry.status !== "running",
  ).length
  const seen = useRef(reranked)
  const wasRunning = useRef(agent.running)
  useEffect(() => {
    const refresh = reranked > seen.current || (wasRunning.current && !agent.running)
    seen.current = reranked
    wasRunning.current = agent.running
    // A step never changes once saved, so only the run itself is read again.
    if (refresh) void client.invalidateQueries({ queryKey: searchRunKey(runId), exact: true })
  }, [client, runId, reranked, agent.running])
  return (
    <StepTrail
      search={search}
      onGoBack={(step) => goBack(search, step)}
      disabled={status === "running" || agent.running}
    />
  )
}
