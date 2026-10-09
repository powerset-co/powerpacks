import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { errorText } from "@/lib/api/http"
import { createSet, deleteSet, fetchSets, SetsError, type SetsPayload } from "@/lib/api/sets"

export const SETS_KEY = ["people", "sets"] as const

/** The sets the owner belongs to, and the three ways they change: refresh, create, delete. */
export function useSets() {
  const client = useQueryClient()
  const sets = useQuery({ queryKey: SETS_KEY, queryFn: () => fetchSets(false) })
  const [error, setError] = useState<SetsError | null>(null)
  const settle = (payload: SetsPayload) => {
    client.setQueryData(SETS_KEY, payload)
    setError(null)
  }
  const fail = (caught: unknown) =>
    setError(caught instanceof SetsError ? caught : new SetsError(errorText(caught), false))
  const refresh = useMutation({ mutationFn: () => fetchSets(true), onSuccess: settle, onError: fail })
  const create = useMutation({ mutationFn: createSet, onSuccess: settle, onError: fail })
  const remove = useMutation({ mutationFn: deleteSet, onSuccess: settle, onError: fail })
  const failure = error ?? (sets.error instanceof SetsError ? sets.error : null)
  return {
    data: sets.data,
    failure,
    fail,
    busy: refresh.isPending || create.isPending || remove.isPending,
    refreshing: refresh.isPending,
    refresh: () => refresh.mutate(),
    create: (name: string) => create.mutateAsync(name),
    remove: (set_id: string) => remove.mutateAsync(set_id),
  }
}

const TARGET_KEY = "people.shareTarget"

/** The set the owner shares to, remembered in this browser; "" is the personal (local) network. */
export function readTarget(): string {
  try {
    return localStorage.getItem(TARGET_KEY) ?? ""
  } catch {
    return ""
  }
}

export function writeTarget(set_id: string): void {
  try {
    localStorage.setItem(TARGET_KEY, set_id)
  } catch {
    // A private window: the choice lasts the page.
  }
}
