import { useQuery } from "@tanstack/react-query"

import { fetchDossier } from "@/lib/api/review"

export type DossierState =
  | { status: "loading" }
  /** The server's HTML fragment. */
  | { status: "ready"; html: string }
  /** The server has no dossier for this person. */
  | { status: "missing" }
  /** The request never got an answer. */
  | { status: "failed" }

const LOADING: DossierState = { status: "loading" }
const MISSING: DossierState = { status: "missing" }
const FAILED: DossierState = { status: "failed" }

/** One person's dossier, read once each time a card (or an opened row) mounts and never kept
 *  after it leaves: the next card of the same person reads it fresh. */
export function useDossier(slug: string): DossierState {
  const query = useQuery({
    queryKey: ["review", "dossier", slug],
    queryFn: ({ signal }) => fetchDossier(slug, signal),
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  if (query.isError) return FAILED
  if (query.data === undefined) return LOADING
  return query.data === null ? MISSING : { status: "ready", html: query.data }
}
