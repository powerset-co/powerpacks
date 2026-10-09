// The chat's Full access tip: shown beside approval requests until dismissed or Full access goes
// on; the dismissal is remembered on this Mac.

import { useState } from "react"

import { readStored, writeStored } from "@/lib/storage"

const TIP_KEY = "chat.fullAccessTip"

/** Whether the tip still shows, and the dismissal that hides it for good. */
export function useFullAccessTip(): { shown: boolean; dismiss: () => void } {
  const [dismissed, setDismissed] = useState(
    () => readStored("local", TIP_KEY, (raw) => (raw === "dismissed" ? raw : null)) !== null,
  )
  return {
    shown: !dismissed,
    dismiss: () => {
      setDismissed(true)
      writeStored("local", TIP_KEY, "dismissed")
    },
  }
}
