import { Button } from "@/components/ui/button"
import type { Approval, ApprovalChoice } from "@/types/agent"

const TITLE: Readonly<Record<Approval["kind"], string>> = {
  command: "Codex wants to run a command",
  files: "Codex wants to edit files",
  permissions: "Codex wants more access",
}

function detail(approval: Approval): string | null {
  if (approval.kind === "command") return approval.command
  if (approval.kind === "permissions") {
    const parts = [approval.network ? "Network access" : "", ...approval.paths].filter(Boolean)
    return parts.length ? parts.join("\n") : null
  }
  return null
}

interface ApprovalCardProps {
  approval: Approval
  onAnswer: (choice: ApprovalChoice) => void
}

/** A Codex request waiting on the user: run once, allow for this conversation, or decline. */
export function ApprovalCard({ approval, onAnswer }: ApprovalCardProps) {
  const shown = detail(approval)
  return (
    <section
      aria-label={TITLE[approval.kind]}
      className="rise-in flex flex-col gap-3 rounded-[var(--radius-m)] border border-[color-mix(in_srgb,var(--warn)_40%,transparent)] bg-card p-4 shadow-[var(--shadow-1)]"
    >
      <div className="flex items-center gap-2">
        <span aria-hidden className="size-2 rounded-full bg-warn" />
        <h2 className="m-0 text-[13px] font-semibold">{TITLE[approval.kind]}</h2>
      </div>
      {approval.reason && <p className="m-0 text-xs text-muted-foreground">{approval.reason}</p>}
      {shown && (
        <pre className="m-0 overflow-x-auto whitespace-pre-wrap rounded-[var(--radius-s)] border border-line bg-background px-3 py-2 font-mono text-[12px] text-foreground">
          {shown}
        </pre>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => onAnswer("once")}>
          Allow
        </Button>
        <Button onClick={() => onAnswer("session")}>Allow for this conversation</Button>
        <Button variant="ghost" onClick={() => onAnswer("decline")}>
          Decline
        </Button>
      </div>
    </section>
  )
}
