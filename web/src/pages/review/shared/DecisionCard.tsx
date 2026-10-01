import { Fragment, useState, type ReactNode } from "react"

import { cn } from "@/lib/utils"

interface DecisionCardProps {
  /** What identifies the contents (the person on the card). A new key swaps them in. */
  cardKey: string
  /** The contents are fading out: a decision is saving or the next card is on its way. */
  swapping: boolean
  className?: string
  children: ReactNode
}

// A decision card keeps its frame mounted while its contents swap. `swapping` fades the
// contents out (and takes no clicks); a new `cardKey` remounts them and, from the first swap
// on, they rise in. The first card just appears with the stage.
export function DecisionCard({ cardKey, swapping, className, children }: DecisionCardProps) {
  const [shown, setShown] = useState(cardKey)
  const [swapped, setSwapped] = useState(false)
  if (cardKey !== shown) {
    setShown(cardKey)
    setSwapped(true)
  }
  return (
    <article
      className={cn("decision-card", className, swapping && "swapping", swapped && "entering")}
      data-card={cardKey}
    >
      <Fragment key={cardKey}>{children}</Fragment>
    </article>
  )
}
