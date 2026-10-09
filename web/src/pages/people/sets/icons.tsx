/** The personal network, on this computer. */
export function HomeIcon({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M2.5 7.5 8 3l5.5 4.5V13h-4v-3.5h-3V13h-4z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/** A set: people who see each other's shared networks. */
export function SetIcon({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="6" cy="5.5" r="2.25" />
        <path d="M2 13c.4-2.2 2-3.5 4-3.5s3.6 1.3 4 3.5" />
        <path d="M10.5 3.6a2.25 2.25 0 0 1 0 4.1M12 9.8c1.1.5 1.8 1.6 2 3.2" />
      </g>
    </svg>
  )
}
