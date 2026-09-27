import { cva } from "class-variance-authority"

// Token-aligned to the legacy results.css .btn family (btn, btn-primary, btn-ghost, btn-ok, btn-bad).
export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-sm border text-xs font-semibold leading-none cursor-pointer transition-[background-color,border-color,transform,opacity] duration-fast ease-out active:translate-y-px disabled:cursor-default disabled:opacity-45 disabled:translate-y-0 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "border-border bg-surface-2 text-foreground hover:bg-line-strong",
        primary:
          "border-primary-action bg-primary-action text-white hover:border-primary-hover hover:bg-primary-hover",
        ghost:
          "border-transparent bg-transparent text-muted-foreground hover:bg-secondary hover:text-foreground",
        ok: "border-[color-mix(in_srgb,var(--ok)_40%,transparent)] bg-ok-soft text-ok hover:bg-[color-mix(in_srgb,var(--ok)_26%,transparent)]",
        bad: "border-[color-mix(in_srgb,var(--bad)_40%,transparent)] bg-bad-soft text-bad hover:bg-[color-mix(in_srgb,var(--bad)_26%,transparent)]",
        link: "border-transparent text-info underline-offset-4 hover:underline",
      },
      size: {
        default: "min-h-8 px-3 py-1.5",
        sm: "min-h-7 px-2",
        icon: "size-8",
      },
      shape: {
        default: "",
        pill: "rounded-full",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
      shape: "default",
    },
  },
)
