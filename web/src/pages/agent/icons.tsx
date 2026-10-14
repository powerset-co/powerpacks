import type { SVGProps } from "react"

// The Agent page's glyphs. Each is decorative: its host carries the label.

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

export function SendIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M12 19V5M5 12l7-7 7 7" />
    </svg>
  )
}

export function StopIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden {...props}>
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  )
}

export function CheckIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </svg>
  )
}

export function CrossIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  )
}

export function TerminalIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M5 7l5 5-5 5M12 17h7" />
    </svg>
  )
}

export function FileIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </svg>
  )
}

export function SparkIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6.3 6.3l2.8 2.8M14.9 14.9l2.8 2.8M6.3 17.7l2.8-2.8M14.9 9.1l2.8-2.8" />
    </svg>
  )
}

export function SidebarIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <rect x="3" y="4" width="18" height="16" rx="3" />
      <path d="M9 4v16" />
    </svg>
  )
}

export function ComposeIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M12 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6" />
      <path d="M17.5 3.5a2.1 2.1 0 0 1 3 3L13 14l-4 1 1-4z" />
    </svg>
  )
}
