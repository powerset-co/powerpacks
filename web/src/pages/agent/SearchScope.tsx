import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { Select } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { fetchSearchScope, LOCAL_SCOPE, saveSearchScope } from "@/lib/api/searches"
import type { SearchSet } from "@/types/searches"

const KEY = ["search-scope"] as const

function label(set: SearchSet): string {
  return set.is_personal && set.owned ? "Personal network" : set.name
}

/** Where chat searches: one of your Powerset sets, or this Mac's local index when it has one. */
export function SearchScope() {
  const client = useQueryClient()
  const scope = useQuery({ queryKey: KEY, queryFn: ({ signal }) => fetchSearchScope(signal), retry: false })
  const save = useMutation({
    mutationFn: saveSearchScope,
    onSuccess: (next) => client.setQueryData(KEY, next),
  })
  if (!scope.data || (scope.data.sets.length === 0 && !scope.data.local)) return null
  return (
    <span className="relative inline-flex">
      <Select
        aria-label="Search in"
        title="Where searches look"
        value={scope.data.selected}
        disabled={save.isPending}
        onChange={(event) => save.mutate(event.target.value)}
        className="h-[26px] rounded-full border-line bg-transparent pl-2.5 pr-7 text-[11px] text-muted-foreground"
      >
        {/* .env names no set of yours (or none, signed out): searches keep using it until a pick. */}
        {scope.data.selected === "" && (
          <option value="" disabled hidden>
            Pick a set
          </option>
        )}
        {scope.data.sets.length > 0 && (
          <optgroup label="Powerset">
            {scope.data.sets.map((set) => (
              <option key={set.set_id} value={set.set_id}>
                {label(set)}
              </option>
            ))}
          </optgroup>
        )}
        {scope.data.local && (
          <optgroup label="This Mac">
            <option value={LOCAL_SCOPE}>Local index</option>
          </optgroup>
        )}
      </Select>
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
