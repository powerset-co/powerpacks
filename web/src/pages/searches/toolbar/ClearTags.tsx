import { useState } from "react"

import { Button } from "@/components/ui/button"

interface ClearTagsProps {
  onClear: () => void
}

// "Clear all" asks once more ("Clear all? Confirm Cancel") before every tag in the search goes.
export function ClearTags({ onClear }: ClearTagsProps) {
  const [confirming, setConfirming] = useState(false)
  // The first "Clear all" arrives with its toolbar group; after Cancel it rises back in.
  const [asked, setAsked] = useState(false)
  if (!confirming) {
    return (
      <Button
        key="ask"
        variant="ghost"
        size="sm"
        shape="pill"
        className={asked ? "rise-in" : undefined}
        onClick={() => {
          setAsked(true)
          setConfirming(true)
        }}
      >
        Clear all
      </Button>
    )
  }
  return (
    <span key="confirm" className={"rise-in inline-flex items-center gap-1.5 text-xs font-semibold"}>
      Clear all?
      <Button
        variant="bad"
        size="sm"
        shape="pill"
        onClick={() => {
          setConfirming(false)
          onClear()
        }}
      >
        Confirm
      </Button>
      <Button variant="ghost" size="sm" shape="pill" onClick={() => setConfirming(false)}>
        Cancel
      </Button>
    </span>
  )
}
