import { useState } from "react"

import { fetchDossier, fetchWorthDetails, ReviewError } from "@/lib/api/review"
import { EMPTY, toggled } from "@/lib/sets"
import type { ReviewCandidate, ReviewPerson } from "@/types/review"

/** What an opened row shows under its reason, as far as it has been read. */
export type RowDetails =
  | { status: "loading" }
  /** The person with their sources, the profile beside them, and the dossier's HTML. */
  | { status: "ready"; person: ReviewPerson; candidate: ReviewCandidate | null; dossier: string }
  /** The server refused: the person is gone, or has nothing to show. */
  | { status: "missing" }
  /** The request never got an answer. */
  | { status: "failed" }

const LOADING: RowDetails = { status: "loading" }
const MISSING: RowDetails = { status: "missing" }
const FAILED: RowDetails = { status: "failed" }

/** The profile and the dossier together: the row draws them at once, never one without the
 *  other. */
async function readDetails(slug: string): Promise<RowDetails> {
  try {
    const [{ person, candidate }, dossier] = await Promise.all([fetchWorthDetails(slug), fetchDossier(slug)])
    return dossier === null ? MISSING : { status: "ready", person, candidate, dossier }
  } catch (error) {
    return error instanceof ReviewError ? MISSING : FAILED
  }
}

export interface OpenedRows {
  isOpen: (slug: string) => boolean
  /** The row's details as far as they have been read; undefined until the row first opens. */
  detailsOf: (slug: string) => RowDetails | undefined
  /** The row was opened or closed. */
  toggle: (slug: string, open: boolean) => void
}

/**
 * Which rows of a decided pile are open, and the details each one read when it first opened
 * (once per row, never again). The list keeps this, not the row: a row that scrolls out of
 * the list is unmounted, and comes back as it was left.
 */
export function useOpenedRows(): OpenedRows {
  const [open, setOpen] = useState(EMPTY)
  const [details, setDetails] = useState<ReadonlyMap<string, RowDetails>>(new Map())
  const keep = (slug: string, read: RowDetails) => setDetails((held) => new Map(held).set(slug, read))

  function toggle(slug: string, opened: boolean) {
    setOpen((slugs) => toggled(slugs, slug, opened))
    if (!opened || details.has(slug)) return
    keep(slug, LOADING)
    void readDetails(slug).then((read) => keep(slug, read))
  }

  return { isOpen: (slug) => open.has(slug), detailsOf: (slug) => details.get(slug), toggle }
}
