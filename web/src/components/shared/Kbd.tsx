import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface KbdProps {
  children: ReactNode;
  className?: string;
}

export function Kbd({ children, className }: KbdProps) {
  return (
    <kbd
      className={cn(
        "inline-block min-w-[18px] rounded border border-b-2 border-line-strong bg-card px-[5px] py-px text-center font-[ui-monospace,SFMono-Regular,Menlo,monospace] text-[10.5px] leading-[1.4] text-muted-foreground",
        className,
      )}
    >
      {children}
    </kbd>
  );
}
