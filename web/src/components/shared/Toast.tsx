import { useEffect } from "react"

import { usePresence } from "@/hooks/usePresence"
import { cn } from "@/lib/utils"

import { Kbd } from "./Kbd"

const HIDE_AFTER_MS = 6000
const HIDE_ERROR_AFTER_MS = 8000

export interface ToastAction {
  label: string
  // The page shortcut that does the same, when there is one.
  kbd?: string
  onClick: () => void
}

export interface ToastMessage {
  message: string
  error?: boolean
  action?: ToastAction
}

interface ToastProps {
  // A new object re-arms the timer, even for the same text.
  toast: ToastMessage | null
  onDismiss: () => void
  className?: string
}

// It rises in and drops out (index.css .rise), keeping its last message as it leaves.
export function Toast({ toast, onDismiss, className }: ToastProps) {
  const { mounted, open, shown, onTransitionEnd } = usePresence(toast)

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(onDismiss, toast.error ? HIDE_ERROR_AFTER_MS : HIDE_AFTER_MS)
    return () => window.clearTimeout(timer)
  }, [toast, onDismiss])

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-5 right-5 z-[100] max-w-[min(380px,calc(100vw-40px))]"
    >
      {mounted && shown ? (
        <div
          data-open={open}
          aria-hidden={open ? undefined : true}
          onTransitionEnd={onTransitionEnd}
          className={cn(
            "rise flex items-center gap-3 rounded-md border border-border px-3.5 py-2.5 text-xs font-bold",
            shown.error ? "bg-[#7f1d1d] text-white" : "bg-foreground text-background",
            className,
          )}
        >
          <span className="min-w-0 break-words">{shown.message}</span>
          {shown.action ? <ToastButton action={shown.action} /> : null}
        </div>
      ) : null}
    </div>
  )
}

function ToastButton({ action }: { action: ToastAction }) {
  return (
    <button
      type="button"
      onClick={action.onClick}
      className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-[5px] border border-current bg-transparent px-2 py-1 text-inherit disabled:cursor-wait disabled:opacity-60"
    >
      {action.label}
      {action.kbd ? (
        <Kbd className="border-current bg-transparent text-inherit opacity-70">{action.kbd}</Kbd>
      ) : null}
    </button>
  )
}
