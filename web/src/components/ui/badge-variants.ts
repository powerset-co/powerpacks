import { cva } from "class-variance-authority"

// Token-aligned to the legacy results.css .badge family.
export const badgeVariants = cva(
  "inline-flex h-5 items-center gap-[5px] whitespace-nowrap rounded-full px-2 text-[10.5px] font-bold leading-none tracking-[.01em]",
  {
    variants: {
      variant: {
        default: "bg-secondary text-foreground",
        ok: "bg-ok-soft text-ok",
        warn: "bg-warn-soft text-warn",
        bad: "bg-bad-soft text-bad",
        info: "bg-info-soft text-info",
        muted: "bg-secondary text-muted-foreground",
        // The two the cloud app's filter modal uses.
        secondary: "bg-secondary text-foreground",
        outline: "border border-line text-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
)
