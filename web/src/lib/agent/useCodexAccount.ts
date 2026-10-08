// The Codex account: who is signed in, and ChatGPT sign-in through the system browser.
// Shared by the Agent page's gate and the Accounts page's Codex card.

import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useState } from "react"

import {
  cancelCodexLogin,
  fetchCodexStatus,
  logoutCodex,
  onAgentEvent,
  startCodexLogin,
} from "@/lib/api/codex"
import { errorText } from "@/lib/api/http"
import type { CodexStatus } from "@/types/agent"

const CODEX_KEY = ["codex"] as const

export interface CodexAccountState {
  status: CodexStatus | undefined
  loading: boolean
  /** True while the browser sign-in is open. */
  signingIn: boolean
  error: string | null
  connect: () => void
  cancel: () => void
  signOut: () => void
}

export function useCodexAccount(): CodexAccountState {
  const client = useQueryClient()
  const query = useQuery({ queryKey: CODEX_KEY, queryFn: fetchCodexStatus })
  const [loginId, setLoginId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const refresh = useCallback(() => void client.invalidateQueries({ queryKey: CODEX_KEY }), [client])

  useEffect(
    () =>
      onAgentEvent((event) => {
        if (event.type === "loginCompleted") {
          setLoginId(null)
          setActionError(event.success ? null : (event.error ?? "Sign-in did not finish."))
          refresh()
        }
        if (event.type === "accountUpdated") refresh()
      }),
    [refresh],
  )

  const run = (action: Promise<unknown>) => {
    setActionError(null)
    action.catch((error: unknown) => setActionError(errorText(error))).finally(refresh)
  }

  return {
    status: query.data,
    loading: query.isPending,
    signingIn: loginId !== null,
    error: actionError ?? (query.error ? errorText(query.error) : null),
    connect: () => {
      setActionError(null)
      startCodexLogin()
        .then(setLoginId)
        .catch((error: unknown) => setActionError(errorText(error)))
    },
    cancel: () => {
      if (loginId !== null) run(cancelCodexLogin(loginId))
      setLoginId(null)
    },
    signOut: () => run(logoutCodex()),
  }
}
