import { useId } from "react"

import { Tip } from "@/components/shared/Tip"

/** Composer preferences share the existing Full Access pill and hover/focus explanation. */
export function ComposerToggle({
  label,
  enabled,
  disabled,
  onChange,
  description,
}: {
  label: string
  enabled: boolean
  disabled?: boolean
  onChange: () => void
  description: string
}) {
  const help = useId()
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={enabled}
      aria-describedby={help}
      disabled={disabled}
      onClick={onChange}
      className={`group relative flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition-colors duration-fast ease-out disabled:opacity-45 ${
        enabled
          ? "border-[color-mix(in_srgb,var(--warn)_50%,transparent)] text-foreground"
          : "border-line text-faint hover:text-muted-foreground"
      }`}
    >
      <span aria-hidden className={`size-1.5 rounded-full ${enabled ? "bg-warn" : "bg-line-strong"}`} />
      {label}
      <Tip id={help} side="above" className="w-64 whitespace-normal text-left leading-relaxed">
        {description}
      </Tip>
    </button>
  )
}
