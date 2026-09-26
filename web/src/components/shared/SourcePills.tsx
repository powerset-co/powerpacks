import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

import type { Channel } from "@/types/people";
import { SourcePill } from "./SourcePill";

interface SourcePillsProps extends Omit<HTMLAttributes<HTMLSpanElement>, "children"> {
  channels: readonly Channel[];
}

export function SourcePills({ channels, className, ...rest }: SourcePillsProps) {
  return (
    <span className={cn("inline-flex flex-nowrap gap-1 overflow-hidden", className)} {...rest}>
      {channels.map((channel) => (
        <SourcePill key={channel} channel={channel} />
      ))}
    </span>
  );
}
