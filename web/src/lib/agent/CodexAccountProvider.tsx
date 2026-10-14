import type { ReactNode } from "react"

import { CodexAccountContext, useCodexAccountState } from "./useCodexAccount"

export function CodexAccountProvider({ children }: { children: ReactNode }) {
  const account = useCodexAccountState()
  return <CodexAccountContext.Provider value={account}>{children}</CodexAccountContext.Provider>
}
