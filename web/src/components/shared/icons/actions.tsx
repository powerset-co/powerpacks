import type { SVGProps } from "react"

// The action glyphs, one home each: the search page's PLUS_SVG, PIN_SVG and FLAG_SVG
// (results_web/rendering.py); tests/test_icons.py pins them equal. Each is decorative: its
// button carries the label.

type IconProps = SVGProps<SVGSVGElement>

const STROKED = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const

/** The close and remove mark, as text: its host sets the size. */
export const CLOSE_MARK = "×"

export function PlusIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

export function PinIcon(props: IconProps) {
  return (
    <svg {...STROKED} strokeWidth={1.8} {...props}>
      <path d="M16 9V4l1-1V2H7v1l1 1v5l-3 3v2h14v-2zM12 14v8" />
    </svg>
  )
}

export function FlagIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
      <line x1="4" x2="4" y1="22" y2="15" />
    </svg>
  )
}

/** Setup's fresh start: a spark. */
export function SparkIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6" />
    </svg>
  )
}

/** Setup's import: a tray taking a file in. */
export function ImportIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M12 3v11M8 10l4 4 4-4" />
      <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
    </svg>
  )
}
