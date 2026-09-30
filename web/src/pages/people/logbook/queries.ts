// The reader's reads of the saved archive, keyed under SAVED_LOGBOOK_KEY so a finished build
// refreshes them. Nothing refetches on focus: the files change only when a build runs.

import { queryOptions } from "@tanstack/react-query"

import { fetchConversation, fetchLogbookEntries, fetchLogbookEntry } from "@/lib/api/logbook"

import { SAVED_LOGBOOK_KEY } from "../hooks/useLogbook"

const READ = { retry: false, refetchOnWindowFocus: false } as const

export const entriesQuery = queryOptions({
  ...READ,
  queryKey: [...SAVED_LOGBOOK_KEY, "entries"],
  queryFn: fetchLogbookEntries,
})

export function entryQuery(slug: string) {
  return queryOptions({
    ...READ,
    queryKey: [...SAVED_LOGBOOK_KEY, "entry", slug],
    queryFn: ({ signal }) => fetchLogbookEntry(slug, signal),
  })
}

export function conversationQuery(slug: string, path: string) {
  return queryOptions({
    ...READ,
    queryKey: [...SAVED_LOGBOOK_KEY, "conversation", slug, path],
    queryFn: ({ signal }) => fetchConversation(slug, path, signal),
  })
}
