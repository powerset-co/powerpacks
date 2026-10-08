import type { VariantProps } from "class-variance-authority"

import { CHANNELS, type Channel } from "@/lib/channels"
import { cn } from "@/lib/utils"

import { CHANNEL_ICON } from "./icons/channels"
import { sourcePillVariants } from "./source-pill-variants"

export type SourcePillSize = NonNullable<VariantProps<typeof sourcePillVariants>["size"]>

interface SourcePillProps {
  channel: Channel
  size?: SourcePillSize
  // How many emails or messages, already worded ("42", "~2k"); none draws the glyph alone.
  count?: string
  // What hovering says; the channel's name when not given.
  title?: string
}

export function SourcePill({ channel, size, count, title }: SourcePillProps) {
  const Icon = CHANNEL_ICON[channel]
  const { title: name, colors } = CHANNELS[channel]
  return (
    <span data-c={channel} title={title ?? name} className={cn(sourcePillVariants({ size }), colors)}>
      <Icon role="img" aria-label={name} />
      {count}
    </span>
  )
}
