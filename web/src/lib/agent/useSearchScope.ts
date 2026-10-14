// Where chat searches: the signed-in user's Powerset sets and this Mac's local index (search_scope.py).

import { useQuery } from "@tanstack/react-query"

import { fetchSearchScope } from "@/lib/api/searches"

export const SEARCH_SCOPE_KEY = ["search-scope"] as const

/** The chat page asks while Codex starts, so the picker shows with the message box. */
export function useSearchScope() {
  return useQuery({
    queryKey: SEARCH_SCOPE_KEY,
    queryFn: ({ signal }) => fetchSearchScope(signal),
    retry: false,
  })
}
