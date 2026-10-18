import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { body, failure } from "@/lib/api/http"

export interface Features {
  sets: boolean
  auto_reply: boolean
  /** The People page's Share network upload. */
  share: boolean
  /** Remote assist: the Powerset team may work on this install through its agent. */
  support: boolean
  /** The Team page, for working on teammates' installs from this machine. */
  team: boolean
}

const DEFAULTS: Features = { sets: false, auto_reply: false, share: false, support: false, team: false }
const KEY = ["features"] as const

async function request(features?: Features): Promise<Features> {
  const response = await fetch(
    "/api/features",
    features && {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(features),
    },
  )
  if (!response.ok) throw await failure(response, "Couldn't update local features.")
  return body<Features>(response)
}

/** Local release switches; unavailable settings keep experiments off. */
export function useFeatureSettings() {
  return useQuery({ queryKey: KEY, queryFn: () => request(), retry: false, staleTime: Infinity })
}

export function useFeatures(): Features {
  return useFeatureSettings().data ?? DEFAULTS
}

export function useUpdateFeatures() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: request,
    onSuccess: (features) => client.setQueryData(KEY, features),
  })
}
