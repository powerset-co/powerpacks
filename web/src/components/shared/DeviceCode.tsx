import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import type { CodexAccountState } from "@/lib/agent/useCodexAccount"
import { cn } from "@/lib/utils"

import { Spinner } from "./Spinner"

const COPIED_FOR_MS = 2000

/**
 * A ChatGPT device-code sign-in in progress: the code to copy, the device page to open in the
 * browser, and the wait for the user to finish there. Shown by the Chat gate and the Codex card.
 */
export function DeviceCode({ codex, compact = false }: { codex: CodexAccountState; compact?: boolean }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), COPIED_FOR_MS)
    return () => window.clearTimeout(timer)
  }, [copied])
  if (!codex.login) return null
  const copy = () => {
    void navigator.clipboard.writeText(codex.login?.userCode ?? "").then(() => setCopied(true))
  }
  return (
    <div
      data-device-code
      className={cn("flex w-full flex-col gap-3", compact ? "items-start" : "items-center")}
    >
      <p className={cn("m-0 text-muted-foreground", compact ? "text-xs" : "text-sm")}>
        Enter this code on ChatGPT&apos;s device page.
      </p>
      <code
        className={cn(
          "select-all rounded-[var(--radius-m)] border border-line bg-surface-2 font-mono font-semibold tracking-[.14em] text-foreground",
          compact ? "px-3 py-1.5 text-lg" : "px-5 py-3 text-[28px]",
        )}
      >
        {codex.login.userCode}
      </code>
      <div className="flex flex-wrap items-center gap-2">
        <Button size={compact ? "sm" : "default"} onClick={copy} className={cn(copied && "text-ok")}>
          {copied ? "Copied" : "Copy code"}
        </Button>
        <Button size={compact ? "sm" : "default"} variant="primary" onClick={codex.openBrowser}>
          Open ChatGPT
        </Button>
      </div>
      <div aria-live="polite" className="flex items-center gap-2 text-xs text-muted-foreground">
        <Spinner />
        Waiting for you to finish in the browser…
        <Button size="sm" variant="ghost" onClick={codex.cancel}>
          Cancel
        </Button>
      </div>
    </div>
  )
}
