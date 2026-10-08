import type { ReactNode } from "react"

import { EmptyState, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import type { CodexAccountState } from "@/lib/agent/useCodexAccount"

const INSTALL_COMMAND = "npm install -g @openai/codex"

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rise-in mx-auto my-16 flex w-full max-w-[440px] flex-col items-center gap-4 rounded-[var(--radius-l)] border border-line bg-card px-6 py-8 text-center shadow-[var(--shadow-1)]">
      <span aria-hidden className="size-3 rounded-[4px] bg-primary shadow-[0_0_0_4px_var(--primary-soft)]" />
      <h1 className="m-0 text-lg font-semibold">{title}</h1>
      {children}
    </section>
  )
}

/** The Agent page before Codex can run: not installed, signed out, or signing in. */
export function CodexGate({ codex }: { codex: CodexAccountState }) {
  if (codex.loading) return <EmptyState>Checking Codex…</EmptyState>
  if (!codex.status?.installed) {
    return (
      <Panel title="Install Codex">
        <p className="m-0 text-muted-foreground">
          The agent runs on the Codex CLI. Install it, then reopen this tab.
        </p>
        <code className="rounded-[var(--radius-s)] border border-line bg-background px-3 py-2 font-mono text-xs">
          {INSTALL_COMMAND}
        </code>
        {codex.error && <p className="m-0 text-xs text-bad">{codex.error}</p>}
      </Panel>
    )
  }
  return (
    <Panel title="Connect your ChatGPT account">
      <p className="m-0 text-muted-foreground">
        Codex signs in with your ChatGPT plan. The sign-in page opens in your browser and this tab continues
        when it finishes.
      </p>
      {codex.signingIn ? (
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <Spinner />
          Finish signing in in your browser
          <Button size="sm" variant="ghost" onClick={codex.cancel}>
            Cancel
          </Button>
        </div>
      ) : (
        <Button variant="primary" onClick={codex.connect}>
          Connect ChatGPT
        </Button>
      )}
      {codex.error && <p className="m-0 text-xs text-bad">{codex.error}</p>}
    </Panel>
  )
}
