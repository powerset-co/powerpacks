// The Codex account: who is signed in, and ChatGPT sign-in: the app opens ChatGPT's sign-in
// page in the system browser and Codex reports the sign-in done. Shared by the Agent page's gate and the Accounts page's Codex card.

import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useState } from "react"

import {
  cancelCodexLogin,
  type CodexLogin,
  fetchCodexStatus,
  logoutCodex,
  onAgentEvent,
  startCodexLogin,
} from "@/lib/api/codex"
import { errorText } from "@/lib/api/http"
import { focusApp, openExternal } from "@/lib/api/install"
import type { CodexStatus } from "@/types/agent"

const CODEX_KEY = ["codex"] as const

export interface CodexAccountState {
  status: CodexStatus | undefined
  loading: boolean
  /** The sign-in in progress; null when none. */
  login: CodexLogin | null
  error: string | null
  connect: () => void
  /** Open the sign-in page in the system browser (again). */
  openBrowser: () => void
  cancel: () => void
  signOut: () => void
  /** Ask Codex again after it failed to answer. */
  retry: () => void
}

export function useCodexAccount(): CodexAccountState {
  const client = useQueryClient()
  const query = useQuery({ queryKey: CODEX_KEY, queryFn: fetchCodexStatus })
  const [login, setLogin] = useState<CodexLogin | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const refresh = useCallback(() => void client.invalidateQueries({ queryKey: CODEX_KEY }), [client])

  useEffect(
    () =>
      onAgentEvent((event) => {
        if (event.type === "loginCompleted") {
          void focusApp()
          setLogin(null)
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

  const failed = (error: unknown) => setActionError(errorText(error))

  return {
    status: query.data,
    loading: query.isPending,
    login,
    error: actionError ?? (query.error ? errorText(query.error) : null),
    connect: () => {
      setActionError(null)
      startCodexLogin()
        .then((started) => {
          setLogin(started)
          return openExternal(started.authUrl)
        })
        .catch(failed)
    },
    openBrowser: () => {
      if (login) openExternal(login.authUrl).catch(failed)
    },
    cancel: () => {
      if (login) run(cancelCodexLogin(login.loginId))
      setLogin(null)
    },
    signOut: () => run(logoutCodex()),
    retry: () => {
      setActionError(null)
      refresh()
    },
  }
}
