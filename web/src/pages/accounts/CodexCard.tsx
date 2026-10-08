import { Spinner } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import type { CodexStatus } from "@/types/agent"

function summary(status: CodexStatus | undefined): { name: string; plan: string; connected: boolean } {
  if (!status?.installed) return { name: "Missing from this app", plan: "—", connected: false }
  const account = status.account
  if (account === null) return { name: "Not signed in", plan: "—", connected: false }
  if (account.kind === "chatgpt")
    return { name: account.email ?? "ChatGPT", plan: account.plan, connected: true }
  return { name: account.kind === "apiKey" ? "API key" : "Signed in", plan: "—", connected: true }
}

/** The Codex account that runs Chat, signed in with ChatGPT. Desktop app only. */
export function CodexCard() {
  const codex = useCodexAccount()
  const { name, plan, connected } = summary(codex.status)
  return (
    <li
      data-account="codex"
      className="rounded-[var(--radius-l)] border border-line bg-card p-4 shadow-[var(--shadow-1)]"
    >
      <div className="flex items-center gap-2.5">
        <span
          aria-hidden
          className="grid size-6 place-items-center rounded-[var(--radius-s)] border border-line bg-surface-2 font-mono text-[11px] font-bold text-foreground"
        >
          &gt;_
        </span>
        <span className="text-sm font-semibold">Codex</span>
        <span className="min-w-0 truncate text-xs text-muted-foreground">{name}</span>
        <Badge variant={connected ? "ok" : "muted"} className="ml-auto">
          {connected ? "Connected" : "Not connected"}
        </Badge>
      </div>
      <div className="mb-3 mt-2 flex min-h-7 items-center justify-between gap-3">
        <p aria-live="polite" className="m-0 flex items-center gap-2 text-xs text-muted-foreground">
          {codex.signingIn && <Spinner />}
          {codex.signingIn ? "Finish signing in above." : "Runs Chat on your ChatGPT plan."}
        </p>
        <CodexAction codex={codex} connected={connected} />
      </div>
      {codex.error && <p className="m-0 mb-3 text-xs text-bad">{codex.error}</p>}
      <dl className="m-0 grid grid-cols-4 gap-3 max-[680px]:grid-cols-2">
        <div className="flex flex-col gap-0.5">
          <dt className="text-[10.5px] font-semibold uppercase tracking-[.08em] text-faint">Plan</dt>
          <dd className="m-0 text-sm capitalize">{plan}</dd>
        </div>
      </dl>
    </li>
  )
}

function CodexAction({
  codex,
  connected,
}: {
  codex: ReturnType<typeof useCodexAccount>
  connected: boolean
}) {
  if (codex.loading || !codex.status?.installed) return null
  if (codex.signingIn) {
    return (
      <Button size="sm" variant="ghost" onClick={codex.cancel}>
        Cancel
      </Button>
    )
  }
  return connected ? (
    <Button size="sm" variant="ghost" onClick={codex.signOut}>
      Sign out
    </Button>
  ) : (
    <Button size="sm" variant="primary" onClick={codex.connect}>
      Connect ChatGPT
    </Button>
  )
}
