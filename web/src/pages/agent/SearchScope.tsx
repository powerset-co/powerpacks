import { useEffect } from "react"

import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "@/components/ui/select"
import { setScope, useAgent } from "@/lib/agent/store"
import { useSearchScope } from "@/lib/agent/useSearchScope"
import type { ChatScope, SearchSet } from "@/types/searches"

const LOCAL = "local"

function name(set: SearchSet): string {
  return set.is_personal && set.owned ? "Personal network" : set.name
}

function people(set: SearchSet): string {
  return ` · ${set.person_count.toLocaleString()}`
}

/** The picker's value for a scope: the set id, or "local". */
function valueOf(scope: ChatScope | null): string {
  if (scope === null) return ""
  return "set_id" in scope ? scope.set_id : LOCAL
}

/** Where this chat searches: one of your Powerset sets, or this Mac's local index when it has one.
 *  Picked before the first message and pinned from then on; a different scope is a new chat. A new
 *  chat starts where the last one searched, else on the local index, else on the largest set. */
export function SearchScope() {
  const agent = useAgent()
  const scope = useSearchScope()
  const sets = scope.data?.sets ?? []
  const local_db = scope.data?.local_db ?? null
  const pinned = agent.threadId !== null
  // A pick still on offer, else the local index, else the largest set.
  const offered = (pick: ChatScope | null): pick is ChatScope =>
    pick !== null &&
    ("local_db" in pick ? local_db !== null : sets.some(({ set_id }) => set_id === pick.set_id))
  const fallback: ChatScope | null = local_db ? { local_db } : sets[0] ? { set_id: sets[0].set_id } : null
  const current = pinned || offered(agent.scope) ? agent.scope : fallback
  // A new chat with no pick on offer takes the fallback, so its first message pins it.
  useEffect(() => {
    if (!pinned && current !== null && current !== agent.scope) setScope(current)
  }, [pinned, current, agent.scope])
  if (!scope.data || (sets.length === 0 && !local_db)) return null
  const selected = valueOf(current)
  const picked = sets.find(({ set_id }) => set_id === selected)
  const shown =
    selected === LOCAL ? "Local index" : picked ? name(picked) : pinned ? "Set from .env" : "Pick a set"
  const choose = (value: string) => {
    if (value === LOCAL) {
      if (local_db) setScope({ local_db })
    } else setScope({ set_id: value })
  }
  // The pill is the app's Select with its own face: the pick's name and count, as wide as they are.
  return (
    <span className="relative inline-flex">
      <Select value={selected} onValueChange={choose} disabled={pinned}>
        <SelectTrigger
          aria-label="Search in"
          title={
            pinned
              ? "This chat searches here; start a new chat to search elsewhere"
              : "Where this chat searches"
          }
          className="h-[26px] w-auto max-w-[240px] gap-1.5 rounded-full border-line bg-transparent py-0 pl-2.5 pr-2 text-[11px] text-muted-foreground transition-colors duration-fast ease-out hover:text-foreground focus:border-line-strong focus:ring-0 focus:ring-offset-0 disabled:cursor-default disabled:opacity-100 [&>svg]:size-3 [&>svg]:shrink-0"
        >
          {/* A long name gives way before its count does. */}
          <span className="flex min-w-0">
            <span className="truncate">{shown}</span>
            {picked && <span className="shrink-0 whitespace-pre">{people(picked)}</span>}
          </span>
        </SelectTrigger>
        <SelectContent align="start">
          {sets.length > 0 && (
            <SelectGroup>
              <SelectLabel>Powerset</SelectLabel>
              {sets.map((set) => (
                <SelectItem key={set.set_id} value={set.set_id}>
                  {name(set) + people(set)}
                </SelectItem>
              ))}
            </SelectGroup>
          )}
          {local_db && (
            <SelectGroup>
              <SelectLabel>This Mac</SelectLabel>
              <SelectItem value={LOCAL}>Local index</SelectItem>
            </SelectGroup>
          )}
        </SelectContent>
      </Select>
    </span>
  )
}
