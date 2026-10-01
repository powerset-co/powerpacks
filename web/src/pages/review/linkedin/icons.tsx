// The feedback popover's two glyphs (reconcile_review.js SEND_ICON, CHECK_ICON). Decorative:
// the send button carries the label.

const STROKED = {
  viewBox: "0 0 24 24",
  width: 14,
  height: 14,
  fill: "none",
  stroke: "currentColor",
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const

export function SendIcon() {
  return (
    <svg {...STROKED} strokeWidth={2}>
      <path d="m22 2-7 20-4-9-9-4Z" />
      <path d="M22 2 11 13" />
    </svg>
  )
}

export function CheckIcon() {
  return (
    <svg {...STROKED} strokeWidth={2.5}>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  )
}
