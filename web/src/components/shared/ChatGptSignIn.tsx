import { Button } from "@/components/ui/button"
import type { CodexAccountState } from "@/lib/agent/useCodexAccount"
import { cn } from "@/lib/utils"

import { Spinner } from "./Spinner"

/**
 * A ChatGPT sign-in in progress: the wait for the user to finish in the browser, with a way to
 * open the page again. Shown by the Chat gate and the Codex card.
 */
export function ChatGptSignIn({ codex, compact = false }: { codex: CodexAccountState; compact?: boolean }) {
  if (!codex.login) return null
  return (
    <div
      data-chatgpt-signin
      className={cn("flex w-full flex-col gap-3", compact ? "items-start" : "items-center")}
    >
      <div aria-live="polite" className="flex items-center gap-2 text-xs text-muted-foreground">
        <Spinner />
        Finish signing in to ChatGPT in your browser…
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size={compact ? "sm" : "default"} variant="primary" onClick={codex.openBrowser}>
          Open ChatGPT again
        </Button>
        <Button size={compact ? "sm" : "default"} variant="ghost" onClick={codex.cancel}>
          Cancel
        </Button>
      </div>
    </div>
  )
}
