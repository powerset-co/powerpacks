import { useMutation, useQueryClient } from "@tanstack/react-query"

import { errorText } from "@/lib/api/http"
import { SEARCH_SCOPE_KEY as KEY, useSearchScope } from "@/lib/agent/useSearchScope"
import { LOCAL_SCOPE, saveSearchScope } from "@/lib/api/searches"
import type { SearchScope as Scope, SearchSet } from "@/types/searches"

function name(set: SearchSet): string {
  return set.is_personal && set.owned ? "Personal network" : set.name
}

function people(set: SearchSet): string {
  return ` · ${set.person_count.toLocaleString()}`
}

/** Where chat searches: one of your Powerset sets, or this Mac's local index when it has one. */
export function SearchScope() {
  const client = useQueryClient()
  const scope = useSearchScope()
  // The pick shows at once; the server's answer replaces it, or the old pick returns on failure.
  const save = useMutation({
    mutationFn: saveSearchScope,
    onMutate: async (selected: string) => {
      await client.cancelQueries({ queryKey: KEY })
      const before = client.getQueryData<Scope>(KEY)
      client.setQueryData<Scope>(KEY, (old) => old && { ...old, selected })
      return before
    },
    onError: (_error, _selected, before) => client.setQueryData(KEY, before),
    onSuccess: (next) => client.setQueryData(KEY, next),
  })
  if (!scope.data || (scope.data.sets.length === 0 && !scope.data.local)) return null
  const { sets, local, selected } = scope.data
  const picked = sets.find(({ set_id }) => set_id === selected)
  const shown = selected === LOCAL_SCOPE ? "Local index" : picked ? name(picked) : "Pick a set"
  // The pill is as wide as the pick; a native select is as wide as its longest option, so it sits
  // invisibly over the pill and opens the usual menu.
  return (
    <span className="relative inline-flex">
      <span className="relative inline-flex h-[26px] max-w-[240px] items-center gap-1.5 rounded-full border border-line pl-2.5 pr-2 text-[11px] text-muted-foreground transition-colors duration-fast ease-out focus-within:border-line-strong hover:text-foreground">
        {/* A long name gives way before its count does. */}
        <span aria-hidden className="flex min-w-0">
          <span className="truncate">{shown}</span>
          {picked && <span className="shrink-0 whitespace-pre">{people(picked)}</span>}
        </span>
        <svg viewBox="0 0 16 16" aria-hidden="true" className="size-3 shrink-0">
          <path
            d="M4 6l4 4 4-4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <select
          aria-label="Search in"
          title="Where searches look"
          value={selected}
          onChange={(event) => save.mutate(event.target.value)}
          className="absolute inset-0 size-full cursor-pointer appearance-none opacity-0"
        >
          {/* Signed out with a local index not picked yet. */}
          {selected === "" && (
            <option value="" disabled hidden>
              Pick a set
            </option>
          )}
          {sets.length > 0 && (
            <optgroup label="Powerset">
              {sets.map((set) => (
                <option key={set.set_id} value={set.set_id}>
                  {name(set) + people(set)}
                </option>
              ))}
            </optgroup>
          )}
          {local && (
            <optgroup label="This Mac">
              <option value={LOCAL_SCOPE}>Local index</option>
            </optgroup>
          )}
        </select>
      </span>
      {save.error && (
        <span
          role="alert"
          className="absolute bottom-full mb-2 w-64 rounded-md border border-line bg-card p-2 text-xs text-bad"
        >
          {errorText(save.error)}
        </span>
      )}
    </span>
  )
}
