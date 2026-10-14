// The past chats, shared by the chat sidebar and the chat header through one query.

import { useQuery, type UseQueryResult } from "@tanstack/react-query"

import { fetchThreads } from "@/lib/api/codex"
import type { ThreadSummary } from "@/types/agent"

export const THREADS_KEY = ["codex-threads"] as const

export function useThreads(): UseQueryResult<ThreadSummary[]> {
  return useQuery({ queryKey: THREADS_KEY, queryFn: fetchThreads })
}
