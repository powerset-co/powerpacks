import type { CSSProperties, ReactNode } from "react"

import { cn } from "@/lib/utils"

import "./ReservedLines.css"

interface ReservedLinesProps {
  /** Lines held on a wide screen. */
  lines: number
  /** Lines held on a narrow screen (520px or less), where the same text wraps more. */
  narrowLines?: number
  className?: string
  children?: ReactNode
}

// A paragraph that always takes `lines` lines, empty or not, so what sits below it never jumps;
// longer text is cut at the last line. Say a full sentence in fewer lines rather than lean on the cut.
export function ReservedLines({ lines, narrowLines = lines, className, children }: ReservedLinesProps) {
  const style: CSSProperties = { "--reserved-lines-wide": lines, "--reserved-lines-narrow": narrowLines }
  return (
    <p className={cn("reserved-lines", className)} style={style}>
      {children}
    </p>
  )
}
