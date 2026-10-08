import type { HTMLAttributes } from "react"

import type { Channel } from "@/lib/channels"
import { cn } from "@/lib/utils"

import { SourcePill, type SourcePillSize } from "./SourcePill"

interface SourcePillsProps extends Omit<HTMLAttributes<HTMLSpanElement>, "children"> {
  channels: readonly Channel[]
  size?: SourcePillSize
  // A count to draw on a channel's pill (SourcePill `count`).
  counts?: Partial<Record<Channel, string>>
  // What hovering a channel's pill says (SourcePill `title`).
  titles?: Partial<Record<Channel, string>>
}

export function SourcePills({ channels, size, counts, titles, className, ...rest }: SourcePillsProps) {
  return (
    <span className={cn("inline-flex flex-nowrap gap-1 overflow-hidden", className)} {...rest}>
      {channels.map((channel) => (
        <SourcePill
          key={channel}
          channel={channel}
          size={size}
          count={counts?.[channel]}
          title={titles?.[channel]}
        />
      ))}
    </span>
  )
}
