import { ChatGptSignIn } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import type { CodexStatus } from "@/types/agent"

// ChatGPT's plan types as people know them: the Pro tiers are "prolite", "pro" and "promax"
// (the names codex-pooler shows); every other type reads as its own word.
const PLAN_NAMES: Record<string, string> = { prolite: "Pro 100", pro: "Pro 200", promax: "Pro 500" }

function planName(plan: string): string {
  return PLAN_NAMES[plan] ?? plan.charAt(0).toUpperCase() + plan.slice(1)
}

function summary(status: CodexStatus | undefined): { name: string; plan: string | null; connected: boolean } {
  if (!status) return { name: "Could not connect to Codex", plan: null, connected: false }
  if (!status.installed) return { name: "Missing from this app", plan: null, connected: false }
  const account = status.account
  if (account === null) return { name: "Not signed in", plan: null, connected: false }
  if (account.kind === "chatgpt")
    return { name: account.email ?? "ChatGPT", plan: planName(account.plan), connected: true }
  return { name: account.kind === "apiKey" ? "API key" : "Signed in", plan: null, connected: true }
}

/** The Codex account that runs Chat, signed in with ChatGPT; its plan as a badge beside it. Desktop app only. */
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
        {plan && !codex.loading ? <Badge variant="muted">{plan}</Badge> : null}
        <span className="min-w-0 truncate text-xs text-muted-foreground">
          {codex.loading ? "Checking Codex…" : name}
        </span>
        <Badge variant={connected ? "ok" : "muted"} className="ml-auto">
          {codex.loading ? "Checking" : connected ? "Connected" : "Not connected"}
        </Badge>
      </div>
      {codex.login ? (
        <div className="mt-3">
          <ChatGptSignIn codex={codex} compact />
        </div>
      ) : (
        <div className="mt-2 flex min-h-7 items-center justify-between gap-3">
          <p className="m-0 text-xs text-muted-foreground">
            {codex.status?.installed && codex.status.account?.kind === "apiKey"
              ? "Uses your API key. API usage is billed separately."
              : connected
                ? "Runs Chat with your connected account."
                : "Connect ChatGPT to run Chat on your plan."}
          </p>
          <CodexAction codex={codex} connected={connected} />
        </div>
      )}
      {codex.error && <p className="m-0 mt-2 text-xs text-bad">{codex.error}</p>}
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
  if (codex.loading) return null
  if (!codex.status)
    return (
      <Button size="sm" variant="ghost" onClick={codex.retry}>
        Try again
      </Button>
    )
  if (!codex.status.installed) return null
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
