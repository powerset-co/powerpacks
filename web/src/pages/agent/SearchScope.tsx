import { useMutation, useQueryClient } from "@tanstack/react-query"

import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "@/components/ui/select"
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
  // The pill is the app's Select with its own face: the pick's name and count, as wide as they are.
  return (
    <span className="relative inline-flex">
      <Select value={selected} onValueChange={(value) => save.mutate(value)}>
        <SelectTrigger
          aria-label="Search in"
          title="Where searches look"
          className="h-[26px] w-auto max-w-[240px] gap-1.5 rounded-full border-line bg-transparent py-0 pl-2.5 pr-2 text-[11px] text-muted-foreground transition-colors duration-fast ease-out hover:text-foreground focus:border-line-strong focus:ring-0 focus:ring-offset-0 [&>svg]:size-3 [&>svg]:shrink-0"
        >
          {/* A long name gives way before its count does. "Pick a set": signed out with a local index not picked yet. */}
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
          {local && (
            <SelectGroup>
              <SelectLabel>This Mac</SelectLabel>
              <SelectItem value={LOCAL_SCOPE}>Local index</SelectItem>
            </SelectGroup>
          )}
        </SelectContent>
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
