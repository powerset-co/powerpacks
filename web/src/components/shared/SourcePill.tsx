import type { VariantProps } from "class-variance-authority"

import { CHANNELS, type Channel } from "@/lib/channels"
import { cn } from "@/lib/utils"

import { CHANNEL_ICON } from "./icons/channels"
import { sourcePillVariants } from "./source-pill-variants"

export type SourcePillSize = NonNullable<VariantProps<typeof sourcePillVariants>["size"]>

interface SourcePillProps {
  channel: Channel
  size?: SourcePillSize
}

export function SourcePill({ channel, size }: SourcePillProps) {
  const Icon = CHANNEL_ICON[channel]
  const { title, colors } = CHANNELS[channel]
  return (
    <span data-c={channel} title={title} className={cn(sourcePillVariants({ size }), colors)}>
      <Icon role="img" aria-label={title} />
    </span>
  )
}
