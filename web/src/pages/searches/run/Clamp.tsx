import { ChevronDown } from "lucide-react"
import { useLayoutEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"

interface ClampProps {
  text: string
  className?: string
}

/**
 * Text cut to three lines, with a Show more button under it only when the text runs past them
 * (measured, so a short paragraph never offers one). The button opens the rest and turns into
 * Show less. A remount (the drawer's next person) starts it shut.
 */
export function Clamp({ text, className }: ClampProps) {
  const body = useRef<HTMLParagraphElement>(null)
  const [open, setOpen] = useState(false)
  const [overflows, setOverflows] = useState(false)

  useLayoutEffect(() => {
    const element = body.current
    if (!element || open) return
    const measure = () => setOverflows(element.scrollHeight > element.clientHeight + 1)
    measure()
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [text, open])

  return (
    <>
      <p ref={body} className={cn(className, !open && "clamp-3")} data-clamped={!open}>
        {text}
      </p>
      {overflows || open ? (
        <button type="button" className="show-more" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show more"}
          <ChevronDown aria-hidden="true" data-open={open} />
        </button>
      ) : null}
    </>
  )
}
