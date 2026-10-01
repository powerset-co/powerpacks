import { useCallback, useEffect, useState } from "react"

import type { ToastMessage } from "@/components/shared"
import { TOAST_ERROR_MS, TOAST_MS } from "@/lib/review/timing"

/**
 * The page's one toast on its own clock: a message for 1.8 s, an error for 6 s, a new one
 * replacing the last. The shared `Toast` draws it; its own (longer) timer never gets to fire.
 */
export function useReviewToast() {
  const [toast, setToast] = useState<ToastMessage | null>(null)
  const dismiss = useCallback(() => setToast(null), [])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(dismiss, toast.error ? TOAST_ERROR_MS : TOAST_MS)
    return () => window.clearTimeout(timer)
  }, [toast, dismiss])

  const say = useCallback((message: string) => setToast({ message }), [])
  const sayError = useCallback((message: string) => setToast({ message, error: true }), [])

  return { toast, dismiss, say, sayError }
}

export type ReviewToast = ReturnType<typeof useReviewToast>
