import { CHANNEL_TITLE } from "@/lib/people/copy";
import { cn } from "@/lib/utils";
import type { Channel } from "@/types/people";

import { CHANNEL_ICON } from "./icons/channels";

// people.css .source[data-c=…]: tinted fill, bright glyph, half-strength border.
export const SOURCE_PILL_COLOR: Record<Channel, string> = {
  gmail: "bg-[#22c55e33] text-[#4ade80] border-[#22c55e4d]",
  imessage: "bg-[#10b98133] text-[#34d399] border-[#10b9814d]",
  whatsapp: "bg-[#10b98133] text-[#34d399] border-[#10b9814d]",
  linkedin: "bg-[#3b82f633] text-[#60a5fa] border-[#3b82f64d] [&_svg]:size-[13px]",
};

interface SourcePillProps {
  channel: Channel;
}

export function SourcePill({ channel }: SourcePillProps) {
  const Icon = CHANNEL_ICON[channel];
  const title = CHANNEL_TITLE[channel];
  return (
    <span
      data-c={channel}
      title={title}
      className={cn(
        "source inline-flex h-5 items-center justify-center rounded-full border px-[7px] [&_svg]:size-[11px]",
        SOURCE_PILL_COLOR[channel],
      )}
    >
      <Icon role="img" aria-label={title} />
    </span>
  );
}
