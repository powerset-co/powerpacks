import type { SVGProps } from "react"

// The side nav's glyphs, one per page. Decorative: the item carries the label.

type IconProps = SVGProps<SVGSVGElement>

const STROKED = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.75,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const

export function ChatIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-4 3.5V6z" />
    </svg>
  )
}

export function SearchesIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M20 20l-4.2-4.2" />
    </svg>
  )
}

export function PeopleIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <circle cx="9.5" cy="8" r="3.5" />
      <path d="M3.5 19.5a6 6 0 0 1 12 0" />
      <path d="M16 5.2a3.5 3.5 0 0 1 0 5.6M18.5 13.5a6 6 0 0 1 2 6" />
    </svg>
  )
}

/** Two heads side by side: the Powerset team. */
export function TeamIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <circle cx="8" cy="8.5" r="3" />
      <circle cx="16" cy="8.5" r="3" />
      <path d="M2.5 19a5.5 5.5 0 0 1 11 0M10.5 19a5.5 5.5 0 0 1 11 0" />
    </svg>
  )
}

/** A head with a spark of recall: the memory directory. */
export function MemoryIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M5 20v-3.2A7.5 7.5 0 1 1 18.4 13l1.1 2.5h-2V18a2 2 0 0 1-2 2H13" />
      <path d="M11 6.5v3M9.5 8h3M14.5 10.5v2M13.5 11.5h2" />
    </svg>
  )
}

export function TasksIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </svg>
  )
}

export function CollapseIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <rect x="3" y="4" width="18" height="16" rx="3" />
      <path d="M9 4v16M14 10l-2 2 2 2" />
    </svg>
  )
}

export function SettingsIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M4 7.5h8.5M16.5 7.5H20M4 16.5h3.5M11.5 16.5H20" />
      <circle cx="14.5" cy="7.5" r="2" />
      <circle cx="9.5" cy="16.5" r="2" />
    </svg>
  )
}

export function SetupIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 3.5a8.5 8.5 0 0 1 8.5 8.5" strokeWidth={2.5} />
      <path d="M8.5 12.5l2.3 2.3 4.7-4.8" />
    </svg>
  )
}
