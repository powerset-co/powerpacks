import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { errorText } from "@/lib/api/http"
import {
  answerInvite,
  createSet,
  deleteSet,
  fetchSets,
  inviteToSet,
  SetsError,
  type SetsPayload,
} from "@/lib/api/sets"

export const SETS_KEY = ["people", "sets"] as const
// Invites and heartbeats arrive through the relay loop into local files; the kept read is local and cheap.
const POLL_MS = 10_000

/** The sets the owner belongs to, and the ways they change: refresh, create, delete, invite, answer. */
export function useSets() {
  const client = useQueryClient()
  const sets = useQuery({ queryKey: SETS_KEY, queryFn: () => fetchSets(false), refetchInterval: POLL_MS })
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
  const invite = useMutation({
    mutationFn: ({ set_id, email }: { set_id: string; email: string }) => inviteToSet(set_id, email),
    onSuccess: settle,
    onError: fail,
  })
  const reply = useMutation({
    mutationFn: ({ id, accepted }: { id: string; accepted: boolean }) => answerInvite(id, accepted),
    onSuccess: settle,
    onError: fail,
  })
  const failure = error ?? (sets.error instanceof SetsError ? sets.error : null)
  return {
    data: sets.data,
    failure,
    fail,
    busy: refresh.isPending || create.isPending || remove.isPending || invite.isPending || reply.isPending,
    refreshing: refresh.isPending,
    refresh: () => refresh.mutate(),
    create: (name: string) => create.mutateAsync(name),
    remove: (set_id: string) => remove.mutateAsync(set_id),
    invite: (set_id: string, email: string) => invite.mutateAsync({ set_id, email }),
    answer: (id: string, accepted: boolean) => reply.mutate({ id, accepted }),
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
