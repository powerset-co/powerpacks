import { useEffect } from "react"

import { ScopePicker } from "@/components/shared/ScopePicker"
import { setScope, useAgent } from "@/lib/agent/store"
import { defaultScope, offeredSets, useSearchScope } from "@/lib/agent/useSearchScope"
import type { ChatScope } from "@/types/searches"

/** Where this chat searches (the shared Search in picker).
 *  Picked before the first message and pinned from then on; a different scope is a new chat. A new
 *  chat starts where the last one searched, else on the local index, else on the largest set. */
export function SearchScope() {
  const agent = useAgent()
  const scope = useSearchScope()
  const sets = scope.data ? offeredSets(scope.data) : []
  const local_db = scope.data?.local_db ?? null
  // Pinned from the first message on: once sent, the pick is the chat's.
  const pinned = agent.threadId !== null || agent.running
  // A pick still on offer, else the local index, else the largest set.
  const offered = (pick: ChatScope | null): pick is ChatScope =>
    pick !== null &&
    ("local_db" in pick ? local_db !== null : sets.some(({ set_id }) => set_id === pick.set_id))
  const fallback = scope.data ? defaultScope(scope.data) : null
  const current = pinned || offered(agent.scope) ? agent.scope : fallback
  // A new chat with no pick on offer takes the fallback, so its first message pins it.
  useEffect(() => {
    if (!pinned && current !== null && current !== agent.scope) setScope(current)
  }, [pinned, current, agent.scope])
  if (!scope.data || (sets.length === 0 && !local_db)) return null
  // The pill is the app's Select with its own face: the pick's name and count, as wide as they are.
  // Pinned, it is a plain label: no hover, no chevron.
  return (
    <span className="relative inline-flex">
      <ScopePicker
        value={current}
        onChange={setScope}
        disabled={pinned}
        placeholder={pinned ? "Earlier chat" : "Pick a set"}
        title={
          pinned
            ? "This chat searches here; start a new chat to search elsewhere"
            : "Where this chat searches"
        }
        className="h-[26px] w-auto max-w-[240px] gap-1.5 rounded-full border-line bg-transparent py-0 pl-2.5 pr-2 text-[11px] text-muted-foreground transition-colors duration-fast ease-out hover:text-foreground focus:border-line-strong focus:ring-0 focus:ring-offset-0 disabled:cursor-default disabled:opacity-100 disabled:hover:text-muted-foreground [&:disabled>svg]:hidden [&>svg]:size-3 [&>svg]:shrink-0"
      />
    </span>
  )
}
