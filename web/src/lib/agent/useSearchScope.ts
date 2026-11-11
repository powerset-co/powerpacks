// Where chat searches: the signed-in user's Powerset sets and this Mac's local index (search_scope.py).

import { useQuery } from "@tanstack/react-query"

import { fetchSearchScope } from "@/lib/api/searches"
import type { ChatScope, SearchScope, SearchSet } from "@/types/searches"

export const SEARCH_SCOPE_KEY = ["search-scope"] as const

/** The chat page asks while Codex starts, so the picker shows with the message box. */
export function useSearchScope() {
  return useQuery({
    queryKey: SEARCH_SCOPE_KEY,
    queryFn: ({ signal }) => fetchSearchScope(signal),
    retry: false,
  })
}

/** The sets on offer. The local index already holds your own network, so with one on this Mac
 *  the cloud copy of it is not offered. */
export function offeredSets({ sets, local_db }: SearchScope): SearchSet[] {
  return local_db ? sets.filter((set) => !(set.is_personal && set.owned)) : sets
}

/** A set's name as the picker and the Sets page show it. */
export function setName(set: SearchSet): string {
  return set.is_personal && set.owned ? "Personal network" : set.name
}

/** Where a search starts with nothing picked: the local index, else the largest set. */
export function defaultScope(scope: SearchScope): ChatScope | null {
  if (scope.local_db) return { local_db: scope.local_db }
  const largest = offeredSets(scope)[0]
  return largest ? { set_id: largest.set_id } : null
}
