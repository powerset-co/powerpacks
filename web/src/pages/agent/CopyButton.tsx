import { useEffect, useState } from "react"

import { CheckIcon, CopyIcon } from "@/components/shared"
import { cn } from "@/lib/utils"

const COPIED_MS = 1500

interface CopyButtonProps {
  /** What lands on the clipboard: the message as written. */
  text: string
  className?: string
}

/** A ghost button that copies a message; it says "Copied" for a moment afterwards. */
export function CopyButton({ text, className }: CopyButtonProps) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), COPIED_MS)
    return () => window.clearTimeout(timer)
  }, [copied])

  return (
    <button
      type="button"
      aria-label={copied ? "Copied" : "Copy message"}
      title={copied ? "Copied" : "Copy"}
      onClick={() => void navigator.clipboard.writeText(text).then(() => setCopied(true))}
      className={cn(
        "grid size-7 cursor-pointer place-items-center rounded-[var(--radius-s)] border-0 bg-transparent transition-[opacity,background-color,color] duration-fast ease-out hover:bg-secondary",
        copied ? "text-ok" : "text-muted-foreground hover:text-foreground",
        className,
      )}
    >
      {copied ? <CheckIcon className="size-4" /> : <CopyIcon className="size-4" />}
    </button>
  )
}
