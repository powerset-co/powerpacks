import { CHANNELS, type Channel } from "@/lib/channels";
import { cn } from "@/lib/utils";

import { CHANNEL_ICON } from "./icons/channels";

interface SourcePillProps {
  channel: Channel;
}

export function SourcePill({ channel }: SourcePillProps) {
  const Icon = CHANNEL_ICON[channel];
  const { title, colors } = CHANNELS[channel];
  return (
    <span
      data-c={channel}
      title={title}
      className={cn(
        "source inline-flex h-5 items-center justify-center rounded-full border px-[7px] [&_svg]:size-[11px]",
        colors,
      )}
    >
      <Icon role="img" aria-label={title} />
    </span>
  );
}
