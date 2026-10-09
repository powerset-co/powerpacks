import type { ReactNode } from "react"

import { DeviceCode, EmptyState, PowersetMark } from "@/components/shared"
import { Button } from "@/components/ui/button"
import type { CodexAccountState } from "@/lib/agent/useCodexAccount"

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rise-in mx-auto my-16 flex w-full max-w-[440px] flex-col items-center gap-4 rounded-[var(--radius-l)] border border-line bg-card px-6 py-8 text-center shadow-[var(--shadow-1)]">
      <PowersetMark className="size-10 rounded-[10px] shadow-[var(--shadow-1)]" />
      <h1 className="m-0 text-lg font-semibold">{title}</h1>
      {children}
    </section>
  )
}

/** The chat before Codex can run: missing from the app, signed out, or signing in. */
export function CodexGate({ codex }: { codex: CodexAccountState }) {
  if (codex.loading) return <EmptyState>Checking Codex…</EmptyState>
  if (!codex.status) {
    return (
      <Panel title="Codex could not start">
        <p className="m-0 text-muted-foreground">
          Chat runs on the Codex bundled with the app, and it did not answer.
        </p>
        {codex.error && (
          <p className="m-0 break-words text-xs text-bad" data-codex-error>
            {codex.error}
          </p>
        )}
        <Button variant="default" onClick={codex.retry}>
          Try again
        </Button>
      </Panel>
    )
  }
  if (!codex.status.installed) {
    return (
      <Panel title="Chat is unavailable">
        <p className="m-0 text-muted-foreground">
          This copy of Powerpacks is missing Codex. Reinstall the app.
        </p>
      </Panel>
    )
  }
  return (
    <Panel title="Connect your ChatGPT account">
      {codex.login ? (
        <DeviceCode codex={codex} />
      ) : (
        <>
          <p className="m-0 text-muted-foreground">
            Chat runs on Codex with your ChatGPT plan. Sign in with a short code in your browser, and chat
            starts as soon as you finish.
          </p>
          <Button variant="primary" onClick={codex.connect}>
            Connect ChatGPT
          </Button>
        </>
      )}
      {codex.error && <p className="m-0 text-xs text-bad">{codex.error}</p>}
    </Panel>
  )
}
